"""LVA action decoder, migrated from lva_hp_rl_261013/src/model.py.

DINOv3 ViT-L intermediate features + a flow-matching action Transformer, with a ReCap advantage token
and task/object tokens in the cross-attention context. Object conditioning is opt-in;
with object_ids omitted the original state_dict keys are unchanged.
"""
import torch.nn as nn
import torch
import math
from quicforge.models.lva import dinov3 as dino
from quicforge.models.lva.task_ids import validate_task_ids
from quicforge.models.lva.object_ids import object_id_tensor, validate_object_ids

UNCONDITIONAL_ADVANTAGE = 0
POSITIVE_ADVANTAGE = 1
NEGATIVE_ADVANTAGE = 2
NUM_ADVANTAGE_CONDITIONS = 3


class DyT( nn.Module ):
    def __init__( self, emb_dim ):
        super().__init__()
        self._alpha = nn.Parameter(torch.ones(1) * 0.5 )
        self._weight = nn.Parameter( torch.ones(1,emb_dim ) )
        self._bias = nn.Parameter( torch.zeros(1,emb_dim) )

    def forward( self, x ):
        x = torch.tanh( self._alpha * x )
        return x * self._weight + self._bias

class FFN( nn.Module ):
    def __init__( self, emb_dim, drop_ratio ):
        super().__init__()
        self._proj = nn.Linear( emb_dim, emb_dim*4 )
        self._proj2 = nn.Linear( emb_dim*4, emb_dim )
        self._act = nn.SiLU()
        self._drop = nn.Dropout( drop_ratio )

    def forward( self,x ):
        x = self._act( self._proj(x) )
        x = self._proj2( self._drop(x) )
        return x

class AttLayer( nn.Module ):
    def __init__( self, input_dim, emb_dim ):
        super().__init__()
        self._q_proj = nn.Linear( input_dim, emb_dim )
        self._k_proj = nn.Linear( input_dim, emb_dim )
        self._v_proj = nn.Linear( input_dim, emb_dim )
        self._q_norm = DyT( emb_dim )
        self._k_norm = DyT( emb_dim )
        self._norm2 = DyT( emb_dim )
        self._att = nn.MultiheadAttention( emb_dim, 8, batch_first=True )
        self._ffn = FFN( emb_dim, 0.2 )

    def forward( self, x, kv, key_padding_mask=None ):
        q = self._q_proj( x )
        k = self._k_proj( kv )
        v = self._v_proj( kv )

        q = self._q_norm( q )
        k = self._k_norm( k )
        att = self._att(
            q,
            k,
            v,
            key_padding_mask=key_padding_mask,
            need_weights=False,
        )
        x = x + att[0] 
        x = x + self._ffn( self._norm2(x) )
        return x 

class SinusoidalTimeEmbedding(nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        if dim < 4 or dim % 2 != 0:
            raise ValueError(f"time embedding dimension must be even and >= 4, got {dim}")
        self.dim = dim

    def forward(self, t: torch.Tensor) -> torch.Tensor:
        """
        Args:
            t: Tensor of shape (batch_size,) with continuous time values in [0, 1].
        Returns:
            Tensor of shape (batch_size, dim) containing sinusoidal embeddings.
        """
        device = t.device
        half_dim = self.dim // 2
        
        # Calculate frequencies
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, device=device, dtype=torch.float32) * -emb)
        
        # Outer product: t * frequencies
        emb = t[:, None] * emb[None, :]
        
        # Concatenate sin and cos
        emb = torch.cat((emb.sin(), emb.cos()), dim=-1)
        return emb

class AdaLNBlock(nn.Module):
    def __init__(self, hidden_dim: int, time_embed_dim: int):
        super().__init__()
        self.linear1 = nn.Linear(hidden_dim, hidden_dim)
        self.act = nn.SiLU()
        self.linear2 = nn.Linear(hidden_dim, hidden_dim)
        
        self.norm = nn.LayerNorm(hidden_dim, elementwise_affine=False)
        self.time_mlp = nn.Linear(time_embed_dim, hidden_dim * 2)

    def forward(self, x: torch.Tensor, t_emb: torch.Tensor) -> torch.Tensor:
        scale_shift = self.time_mlp(t_emb)
        scale, shift = scale_shift.chunk(2, dim=-1)
        x_norm = self.norm(x) * (1 + scale) + shift
        
        h = self.linear1(x_norm)
        h = self.act(h)
        h = self.linear2(h)
        
        # Residual connection
        return x + h


class ObjectAdaLN(nn.Module):
    """Object-conditioned residual modulation of a time-conditioned query."""

    def __init__(self, emb_dim: int):
        super().__init__()
        self.norm = nn.LayerNorm(emb_dim, elementwise_affine=False)
        self.modulation = nn.Linear(emb_dim, 2 * emb_dim)
        nn.init.zeros_(self.modulation.weight)
        nn.init.zeros_(self.modulation.bias)

    def forward(self, query: torch.Tensor, embedding: torch.Tensor, is_null: torch.Tensor) -> torch.Tensor:
        scale, shift = self.modulation(embedding)[:, None, :].chunk(2, dim=-1)
        residual = scale * self.norm(query) + shift
        return torch.where(is_null[:, None, None], query, query + residual)


class Decoder( nn.Module ):
    def __init__(
            self,
            emb_dim,
            time_dim,
            out_dim,
            n_att_layer,
            n_context,
            num_advantage_conditions=NUM_ADVANTAGE_CONDITIONS,
            task_ids=None,
            dino_weights=None,
            object_ids=None,
            object_conditioning: str = "kv",
            visual_backbone=None,
    ):
        super().__init__()
        objects = validate_object_ids([] if object_ids is None else object_ids)
        if object_conditioning not in ("kv", "adaln"):
            raise ValueError("object_conditioning must be 'kv' or 'adaln'")
        if int(num_advantage_conditions) not in (2, NUM_ADVANTAGE_CONDITIONS):
            raise ValueError(
                "num_advantage_conditions must be 2 (old positive-only checkpoint) "
                f"or {NUM_ADVANTAGE_CONDITIONS} (null/positive/negative), got "
                f"{num_advantage_conditions}"
            )
        # dino_weights=None: random backbone init (overwritten later by a full checkpoint / artifact).
        self._backbone = dino.DINOv3ViTL16() if visual_backbone is None else visual_backbone
        if dino_weights is not None:
            dino.load_weights_from_safetensors(self._backbone, dino_weights)
        visual_dim = 1024 if visual_backbone is None else visual_backbone.embed_dim
        self._emb_proj = nn.Linear( visual_dim, emb_dim )
        self._action_proj = nn.Linear( out_dim, emb_dim )
        self._time_embedding = SinusoidalTimeEmbedding( time_dim )
        self._advantage_embedding = nn.Embedding(
            int(num_advantage_conditions), time_dim
        )
        self._advantage_context_proj = nn.Linear(time_dim, emb_dim, bias=False)
        ids = validate_task_ids([] if task_ids is None else task_ids)
        self._task_embedding = None
        if ids:
            self.register_buffer('_task_ids', torch.tensor(ids, dtype=torch.int32))
            self._task_embedding = nn.Embedding(len(ids), emb_dim)
            nn.init.normal_(self._task_embedding.weight, std=0.02)
        # Row zero is masked from cross-attention. Rows one and two represent
        # ReCap's explicit positive and negative advantage prefix tokens.
        nn.init.zeros_(self._advantage_embedding.weight)
        self._query_pe = nn.Parameter( torch.randn( n_context, emb_dim ) )
        self._ada_ln = nn.ModuleList( [
            AdaLNBlock( emb_dim, time_dim ) for _ in range(n_att_layer)
        ] )
        self._self_att = nn.ModuleList( [AttLayer(emb_dim, emb_dim) for _ in range(n_att_layer)] )
        self._cross_att = nn.ModuleList( [AttLayer(emb_dim, emb_dim) for _ in range(n_att_layer)] )
        self._smoother = nn.ModuleList( [nn.Conv1d( emb_dim, emb_dim, 3, 1, 1 ) for _ in range(n_att_layer)] )

        self._out_proj = nn.Linear( emb_dim, out_dim )
        self._context_pe = nn.Parameter( torch.randn( 3192, emb_dim ) )
        self._n_context = n_context
        self._out_dim = out_dim
        # Allocate after the existing layers to preserve their seeded initialization.
        self._object_embedding = None
        if objects:
            self.register_buffer('_object_ids', torch.tensor(objects, dtype=torch.int32))
            # Row 0 is null; subsequent rows follow the configured object ID table.
            self._object_embedding = nn.Embedding(len(objects) + 1, emb_dim, padding_idx=0)
            nn.init.normal_(self._object_embedding.weight, std=0.02)
            with torch.no_grad():
                self._object_embedding.weight[0].zero_()
        self._object_ada_ln = None
        if objects and object_conditioning == "adaln":
            self._object_ada_ln = nn.ModuleList(ObjectAdaLN(emb_dim) for _ in range(n_att_layer))


    def backbone_forward( self, img ):
        features = self._backbone.forward_with_features(
                img, layers_to_save=[20, 21, 22, 23]
                )['intermediate']
        last_feat = sum( features["layer_{}".format(i)] for i in range(20, 24) )
        return last_feat[:,5:]

    def encode_image( self, img ):
        """Encode an observation once so flow integration can reuse its tokens."""
        context = self.backbone_forward( img )
        if context.shape[1] != self._context_pe.shape[0]:
            raise ValueError(
                f"image produced {context.shape[1]} patch tokens, but Decoder was "
                f"configured for {self._context_pe.shape[0]}"
            )
        return self._emb_proj( context ) + self._context_pe[None]

    def _advantage_condition_ids(
            self, advantage_condition, batch_size, device):
        """Normalize scalar or batched ReCap condition IDs."""
        if advantage_condition is None:
            condition_ids = torch.full(
                (batch_size,),
                UNCONDITIONAL_ADVANTAGE,
                device=device,
                dtype=torch.long,
            )
        else:
            condition_ids = torch.as_tensor(advantage_condition, device=device)
            if condition_ids.ndim == 0:
                condition_ids = condition_ids.expand(batch_size)
            if condition_ids.ndim != 1 or condition_ids.shape[0] != batch_size:
                raise ValueError(
                    f"advantage_condition must be a scalar or shaped ({batch_size},), "
                    f"got {condition_ids.shape}"
                )
            if condition_ids.is_floating_point():
                if not torch.all(torch.isfinite(condition_ids)):
                    raise ValueError("advantage_condition values must be finite")
                if not torch.equal(condition_ids, condition_ids.round()):
                    raise ValueError("advantage_condition values must be integer IDs")
            condition_ids = condition_ids.long()
            num_conditions = self._advantage_embedding.num_embeddings
            if torch.any(condition_ids < 0) or torch.any(
                    condition_ids >= num_conditions):
                labels = "0 (null) or 1 (positive)"
                if num_conditions == NUM_ADVANTAGE_CONDITIONS:
                    labels += " or 2 (negative)"
                raise ValueError(f"advantage_condition values must be {labels}")
        return condition_ids

    def _append_advantage_context(
            self, context, advantage_condition):
        """Append the ReCap advantage token and mask it for null conditioning.

        ReCap represents advantage as a prefix token (``Advantage: positive``),
        not as part of the diffusion timestep. LVA has no language prefix, so a
        learned token in the visual cross-attention context is the corresponding
        conditioning path. Masking the token for the null branch makes that
        branch equivalent to omitting the advantage prompt.
        """
        batch_size = context.shape[0]
        condition_ids = self._advantage_condition_ids(
            advantage_condition,
            batch_size,
            context.device,
        )
        advantage_token = self._advantage_context_proj(
            self._advantage_embedding(condition_ids)
        ).to(dtype=context.dtype)
        conditioned_context = torch.cat(
            (context, advantage_token[:, None, :]),
            dim=1,
        )
        context_padding_mask = torch.zeros(
            batch_size,
            conditioned_context.shape[1],
            device=context.device,
            dtype=torch.bool,
        )
        context_padding_mask[:, -1] = condition_ids.eq(
            UNCONDITIONAL_ADVANTAGE
        )
        return conditioned_context, context_padding_mask

    def predict_velocity(
            self, context, noisy_action, flow_time, advantage_condition=None,
            task_id=None, object_id=None):
        """Predict flow velocity with time and ReCap advantage conditioning.

        Args:
            context: Encoded observation tokens shaped (B, N, D).
            noisy_action: Flow state shaped (B, H, action_dim).
            flow_time: Per-token time shaped (B, H), or one time per batch
                element shaped (B,) / (B, 1).
            advantage_condition: Scalar or (B,) IDs. Zero is unconditional,
                one is positive advantage, and two is negative advantage.
            object_id: Optional scalar or (B,) zero-based categories. None/-1
                disables object conditioning, independently of task/advantage conditions.
        """
        if noisy_action.ndim != 3:
            raise ValueError(f"noisy_action must be (B, H, A), got {noisy_action.shape}")
        batch_size, horizon, action_dim = noisy_action.shape
        if horizon != self._n_context or action_dim != self._out_dim:
            raise ValueError(
                f"expected noisy_action (B, {self._n_context}, {self._out_dim}), "
                f"got {noisy_action.shape}"
            )

        if flow_time.ndim == 1:
            flow_time = flow_time[:, None]
        if flow_time.ndim != 2 or flow_time.shape[0] != batch_size:
            raise ValueError(f"flow_time must be (B,), (B, 1), or (B, H), got {flow_time.shape}")
        if flow_time.shape[1] == 1:
            flow_time = flow_time.expand(-1, horizon)
        elif flow_time.shape[1] != horizon:
            raise ValueError(f"flow_time horizon must be 1 or {horizon}, got {flow_time.shape[1]}")

        query = self._action_proj( noisy_action ) + self._query_pe[None]
        time_emb = self._time_embedding( flow_time.reshape(-1) )
        time_emb = time_emb.reshape( batch_size, horizon, -1 ).to(query.dtype)
        conditioned_context, context_padding_mask = self._append_advantage_context(
            context,
            advantage_condition,
        )
        if getattr(self, '_task_embedding', None) is not None:
            indices = self._task_indices(task_id, batch_size, context.device)
            token = self._task_embedding(indices).to(context.dtype)[:, None, :]
            conditioned_context = torch.cat((conditioned_context, token), dim=1)
            context_padding_mask = torch.cat((context_padding_mask,
                torch.zeros(batch_size, 1, dtype=torch.bool, device=context.device)), dim=1)
        elif task_id is not None:
            raise ValueError('this model has no task conditioning')
        object_embedding = getattr(self, '_object_embedding', None)
        object_ada_ln = getattr(self, '_object_ada_ln', None)
        object_token, object_is_null = None, None
        if object_embedding is not None:
            values = object_id_tensor(object_id, batch_size, context.device)
            matches = values[:, None].eq(self._object_ids[None, :])
            object_is_null = values.eq(-1)
            known = object_is_null | matches.any(dim=1)
            if not torch.compiler.is_compiling() and not torch.jit.is_tracing():
                if not bool(known.all()):
                    raise ValueError('unknown object_id; use the checkpoint object table')
            indices = torch.where(object_is_null, 0, matches.long().argmax(dim=1) + 1)
            indices = torch.where(known, indices, torch.full_like(indices, object_embedding.num_embeddings))
            object_token = object_embedding(indices)
            if object_ada_ln is None:
                token = object_token.to(context.dtype)[:, None, :]
                conditioned_context = torch.cat((conditioned_context, token), dim=1)
                context_padding_mask = torch.cat((context_padding_mask, object_is_null[:, None]), dim=1)
        elif object_id is not None:
            raise ValueError('this model has no object conditioning; set model.object_ids')

        for layer, (ada_ln, sa, ca, sm) in enumerate(zip(
                self._ada_ln, self._self_att, self._cross_att, self._smoother)):
            query = ada_ln( query, time_emb )
            if object_ada_ln is not None:
                query = object_ada_ln[layer](query, object_token, object_is_null)
            query = sa( query, query )
            query = ca(
                query,
                conditioned_context,
                key_padding_mask=context_padding_mask,
            )
            query = sm( query.permute(0,2,1) ).permute(0,2,1)

        return self._out_proj(query)

    def forward(
            self, img, noisy_action, flow_time, advantage_condition=None, task_id=None, object_id=None):
        context = self.encode_image( img )
        kwargs = {} if object_id is None else {'object_id': object_id}
        return self.predict_velocity(
            context, noisy_action, flow_time, advantage_condition, task_id=task_id, **kwargs
        )

    def _task_indices(self, task_id, batch_size, device):
        """Map external INT32 IDs to trained rows without freezing export input."""
        if task_id is None:
            raise ValueError('task_id is required for this multi-task model')
        values = torch.as_tensor(task_id, device=device)
        if values.dtype not in (torch.int32, torch.int64):
            raise ValueError('task_id must use int32 or int64')
        if values.ndim == 0:
            values = values.expand(batch_size)
        if values.ndim != 1 or values.shape[0] != batch_size:
            raise ValueError(f'task_id must be scalar or shaped ({batch_size},)')
        matches = values[:, None].eq(self._task_ids[None, :])
        known = matches.any(dim=1)
        if not torch.compiler.is_compiling() and not torch.jit.is_tracing():
            if not bool(known.all()):
                raise ValueError('unknown task_id; use the checkpoint task table')
        indices = matches.to(torch.int64).argmax(dim=1)
        # Deployment hosts must validate IDs before enqueue. An invalid ID
        # never aliases a valid embedding row in the exported graph.
        return torch.where(known, indices, torch.full_like(indices, self._task_embedding.num_embeddings))

    @torch.no_grad()
    def inference(
            self,
            img,
            action_prefix=None,
            delay=None,
            num_steps=10,
            noise=None,
            guidance_scale=1.0,
            task_id=None,
            object_id=None,
    ):
        """Generate an action chunk with positive-advantage classifier-free guidance.

        ``action_prefix`` must use the same normalized observation-anchored EEF
        convention as training. It can contain only the prefix (B, D, A) or be
        padded to the full horizon (B, H, A). ``delay`` may be a scalar or one
        value per batch item; when omitted it is inferred from the prefix length.
        Guidance follows ReCap: ``(1-w) * v_uncond + w * v_positive``.
        """
        if num_steps <= 0:
            raise ValueError(f"num_steps must be positive, got {num_steps}")
        guidance_scale = float(guidance_scale)
        if not math.isfinite(guidance_scale) or guidance_scale < 0.0:
            raise ValueError(
                f"guidance_scale must be finite and non-negative, got {guidance_scale}"
            )

        batch_size = img.shape[0]
        device = img.device
        dtype = next(self.parameters()).dtype
        if noise is None:
            x_t = torch.randn(
                batch_size, self._n_context, self._out_dim,
                device=device, dtype=dtype,
            )
        else:
            expected = (batch_size, self._n_context, self._out_dim)
            if tuple(noise.shape) != expected:
                raise ValueError(f"noise must have shape {expected}, got {tuple(noise.shape)}")
            x_t = noise.to(device=device, dtype=dtype).clone()

        if action_prefix is None:
            prefix = torch.zeros_like(x_t)
            inferred_delay = 0
        else:
            if action_prefix.ndim != 3 or action_prefix.shape[0] != batch_size:
                raise ValueError(
                    f"action_prefix must be (B, D, A), got {action_prefix.shape}"
                )
            if action_prefix.shape[2] != self._out_dim:
                raise ValueError(
                    f"action_prefix action dim must be {self._out_dim}, "
                    f"got {action_prefix.shape[2]}"
                )
            prefix_len = action_prefix.shape[1]
            if prefix_len > self._n_context:
                raise ValueError(
                    f"action_prefix length {prefix_len} exceeds horizon {self._n_context}"
                )
            prefix = torch.zeros_like(x_t)
            prefix[:, :prefix_len] = action_prefix.to(device=device, dtype=dtype)
            inferred_delay = prefix_len

        if delay is None:
            delay = inferred_delay
        delay = torch.as_tensor(delay, device=device)
        if delay.ndim == 0:
            delay = delay.expand(batch_size)
        if delay.ndim != 1 or delay.shape[0] != batch_size:
            raise ValueError(f"delay must be a scalar or shaped (B,), got {delay.shape}")
        if delay.is_floating_point() and not torch.equal(delay, delay.round()):
            raise ValueError("delay values must be integers")
        delay = delay.long()
        if torch.any(delay < 0) or torch.any(delay > self._n_context):
            raise ValueError(f"delay must be in [0, {self._n_context}]")
        if action_prefix is None and torch.any(delay > 0):
            raise ValueError("a non-zero delay requires action_prefix")
        if action_prefix is not None and torch.any(delay > action_prefix.shape[1]):
            raise ValueError("action_prefix is shorter than at least one requested delay")

        prefix_mask = (
            torch.arange(self._n_context, device=device)[None, :] < delay[:, None]
        )
        prefix_mask_3d = prefix_mask[:, :, None]
        context = self.encode_image( img )
        task_kwargs = {} if task_id is None else {'task_id': task_id}
        if object_id is not None:
            task_kwargs['object_id'] = object_id
        dt = 1.0 / num_steps

        for step in range(num_steps):
            time = step * dt
            x_t = torch.where(prefix_mask_3d, prefix, x_t)
            token_time = torch.full(
                (batch_size, self._n_context), time,
                device=device, dtype=x_t.dtype,
            )
            token_time = torch.where(prefix_mask, torch.ones_like(token_time), token_time)
            if guidance_scale == 0.0:
                velocity = self.predict_velocity(
                    context, x_t, token_time, UNCONDITIONAL_ADVANTAGE, **task_kwargs
                )
            elif guidance_scale == 1.0:
                velocity = self.predict_velocity(
                    context, x_t, token_time, POSITIVE_ADVANTAGE, **task_kwargs
                )
            else:
                velocity_unconditional = self.predict_velocity(
                    context, x_t, token_time, UNCONDITIONAL_ADVANTAGE, **task_kwargs
                )
                velocity_positive = self.predict_velocity(
                    context, x_t, token_time, POSITIVE_ADVANTAGE, **task_kwargs
                )
                velocity = (
                    (1.0 - guidance_scale) * velocity_unconditional
                    + guidance_scale * velocity_positive
                )
            x_t = x_t + dt * velocity

        return torch.where(prefix_mask_3d, prefix, x_t)
