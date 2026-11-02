"""``LVADecoder``: the model class configs name (``model = dict(type="LVADecoder", ...)``).

It is the ported ``Decoder`` (decoder.py, unchanged) plus the config-level options: backbone gradient
checkpointing and the action normalization (``preproc_file``). It adds no parameters, so state_dict keys are
identical and the original checkpoints load with ``strict=True``. The constructor reads no file: pretrained
weights, the whole model's or only the DINOv3 backbone's (``DINOv3Loader``), are loaded after construction from
``model.weights``, like every model's.

``preproc_file`` is the ``preproc.pt`` (action mu / std) the weights are trained with: the model predicts
normalized actions, so the same weights mean nothing with another normalization. It lives in the model section
for that reason; the forward pass does not read it. ``LVADataModule`` takes it from here (``use_model_config``)
to normalize the targets and writes it into the run's ``assets/preproc.pt``, which the Predictor and the
evaluator use. None: computed from the training data (decoder trained from scratch); required with warm-start
``weights`` and in evaluation configs.
"""
from __future__ import annotations

from quicforge.models.lva.decoder import Decoder


class LVADecoder(Decoder):
    """DINOv3 ViT-L + flow-matching action decoder for the observation-anchored bimanual EEF action space."""

    # zero.Init sharded construction has not been validated for this model. Artifacts carry the normalization in
    # assets/preproc.pt.
    quicforge_capabilities = {
        "zero3_init": False,
        "inference_kwargs": {"gradient_checkpointing": False, "preproc_file": None},
    }

    def __init__(self, emb_dim: int = 256, time_dim: int = 32, out_dim: int = 20, n_att_layer: int = 8,
                 n_context: int = 50, task_ids: list[int] | None = None, dino_weights: str | None = None,
                 gradient_checkpointing: bool = False, preproc_file: str | None = None):
        if dino_weights is not None:
            # None is still accepted: the model_config.json of artifacts exported before the change records it
            raise ValueError("model.dino_weights 已移除：DINO 骨干的预训练权重写在 model.weights，"
                             "如 weights=dict(type=\"DINOv3Loader\", path=\"<model.safetensors>\")，"
                             "与整模型权重一样在模型构建后加载并逐键校验")
        if out_dim != 20:
            raise ValueError("观测锚定的双臂 EEF 表示要求 out_dim=20（[position(3), rotation_6d(6), claw(1)] × 2）")
        super().__init__(emb_dim=emb_dim, time_dim=time_dim, out_dim=out_dim, n_att_layer=n_att_layer,
                         n_context=n_context, task_ids=task_ids or [], dino_weights=None)
        self._backbone.gradient_checkpointing = gradient_checkpointing
        self.preproc_file = preproc_file        # used by the data side and the assets, not by forward
