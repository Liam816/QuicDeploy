"""Optional external object IDs, parallel to task IDs; None/-1 denotes null."""
from __future__ import annotations

from numbers import Integral

import torch

from quicforge.models.lva.task_ids import validate_task_ids

NULL_OBJECT_ID = -1


def validate_object_ids(values):
    """The same nonnegative INT32 IDs and explicit table order as task_ids."""
    try:
        return validate_task_ids(values)
    except ValueError as exc:
        raise ValueError(str(exc).replace('task_id', 'object_id')) from exc


def object_id_value(value, allowed):
    """Validate a label scalar without truncating floats or aliasing null to class zero."""
    as_py = getattr(value, "as_py", None)
    if callable(as_py):
        value = as_py()
    if value is None:
        return NULL_OBJECT_ID
    if isinstance(value, bool) or not isinstance(value, Integral) or value not in allowed:
        raise ValueError(f"object_id must be null or an integer in model.object_ids, got {value!r}")
    return int(value)


def object_id_tensor(value, batch_size, device):
    """Accept a scalar, (B,) integer tensor, or a list containing integers/None."""
    if value is None:
        return torch.full((batch_size,), NULL_OBJECT_ID, dtype=torch.int64, device=device)
    if isinstance(value, (list, tuple)):
        converted = []
        for item in value:
            if item is None:
                converted.append(NULL_OBJECT_ID)
            elif isinstance(item, Integral) and not isinstance(item, bool):
                converted.append(int(item))
            else:
                raise ValueError('object_id must contain integers or null')
        value = converted
    values = torch.as_tensor(value, device=device)
    if values.dtype not in (torch.int32, torch.int64):
        raise ValueError("object_id must use int32 or int64 (null is -1 in tensors)")
    if values.ndim == 0:
        values = values.expand(batch_size)
    if values.shape != (batch_size,):
        raise ValueError(f"object_id must be scalar or shaped ({batch_size},)")
    return values.long()


def select_initialization_objects(source, target_ids):
    """Preserve the null row and map learned rows by external ID for warm starts."""
    target_ids = validate_object_ids(target_ids)
    table = source.get('_object_ids')
    embedding = source.get('_object_embedding.weight')
    if table is None and embedding is None:
        return source, None
    if table is None or table.ndim != 1:
        raise ValueError('initialization object embedding requires an object ID table')
    source_ids = validate_object_ids(table.tolist())
    if embedding is None or embedding.ndim != 2 or embedding.shape[0] != len(source_ids) + 1:
        raise ValueError('initialization object embedding rows disagree with object ID table')
    missing = sorted(set(target_ids) - set(source_ids))
    if not target_ids or missing:
        raise ValueError(f'initialization object IDs missing from checkpoint: {missing}; '
                         'an object-conditioned checkpoint requires a nonempty target object table')
    rows = [0] + [source_ids.index(value) + 1 for value in target_ids]
    selected = dict(source)
    selected['_object_embedding.weight'] = embedding[rows].clone()
    selected['_object_ids'] = table.new_tensor(target_ids)
    return selected, rows
