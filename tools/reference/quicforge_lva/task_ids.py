"""Stable external task IDs shared by datasets, checkpoints and deployment (from
lva_hp_rl_261013/src/task_config.py). The task table and dataset sources that the original project read from a
task-manifest JSON file are written directly in the config's ``data`` section and validated here.
"""
from __future__ import annotations

from pathlib import Path


TASK_CONDITIONING_VERSION = "task_context_token_v1"


def task_id_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"task_id must be an integer, got {value!r}")
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid task_id: {value!r}") from exc
    if str(result) != str(value) or not 0 <= result <= 2**31 - 1:
        raise ValueError(f"task_id must be a canonical non-negative INT32, got {value!r}")
    return result


def validate_task_ids(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError("task_ids must be a list")
    result = [task_id_value(value) for value in values]
    if len(result) != len(set(result)):
        raise ValueError("duplicate task_id")
    return result


def checkpoint_task_ids(config):
    ids = validate_task_ids(config.get("task_ids", []))
    if ids and config.get("task_conditioning_version") != TASK_CONDITIONING_VERSION:
        raise ValueError("unknown or missing task conditioning version")
    return ids


def validate_resume_tasks(saved_config, current_config):
    if checkpoint_task_ids(saved_config) != checkpoint_task_ids(current_config):
        raise ValueError("checkpoint task mapping differs; resume requires identical task IDs and order")


def select_initialization_tasks(source, source_ids, target_ids):
    """Select/reorder learned task rows by external ID for weights-only training.

    Never modify the checkpoint tensors in place. Full-state resume continues
    to require identical task tables, since optimizer rows also carry state.
    """
    source_ids = validate_task_ids(source_ids)
    target_ids = validate_task_ids(target_ids)
    if not source_ids:
        return source, None
    missing = sorted(set(target_ids) - set(source_ids))
    if not target_ids or missing:
        raise ValueError(f"initialization task IDs missing from checkpoint: {missing}; "
                         "a task-conditioned checkpoint requires a nonempty target task table")
    task_buffer = source.get('_task_ids')
    embedding = source.get('_task_embedding.weight')
    if task_buffer is None or task_buffer.ndim != 1 or task_buffer.tolist() != source_ids:
        raise ValueError('initialization task buffer disagrees with checkpoint config')
    if embedding is None or embedding.ndim != 2 or embedding.shape[0] != len(source_ids):
        raise ValueError('initialization task embedding rows disagree with checkpoint config')
    rows = [source_ids.index(task_id) for task_id in target_ids]
    selected = dict(source)
    selected['_task_embedding.weight'] = embedding[rows].clone()
    selected['_task_ids'] = task_buffer.new_tensor(target_ids)
    return selected, rows


TASK_KEYS = ("task_id", "task_index", "name")
DATASET_KEYS = ("task_id", "label_file", "video_file", "advantage_file", "advantage")


def _check_keys(item, allowed, where):
    if not isinstance(item, dict):
        raise ValueError(f"{where} entries must be dicts, got {item!r}")
    unknown = sorted(set(item) - set(allowed))
    if unknown:
        raise ValueError(f"unknown {where} keys {unknown}; allowed {list(allowed)}")


def validate_task_manifest(tasks, sources):
    """Validated task table + dataset sources, in the original task-manifest layout
    ``{schema_version, tasks: [{task_id, task_index, name}], datasets: [...]}``.
    """
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks must be a nonempty list")
    for item in tasks:
        _check_keys(item, TASK_KEYS, "tasks")
    ids = validate_task_ids([item["task_id"] for item in tasks])
    normalized_tasks = []
    for index, (item, task_id) in enumerate(zip(tasks, ids, strict=True)):
        if item.get("task_index", index) != index:
            raise ValueError("task_index must match the explicit task table order")
        normalized_tasks.append({"task_id": task_id, "task_index": index,
                                 "name": str(item.get("name", f"task_{task_id}"))})
    datasets = validate_dataset_sources(sources, ids)
    used = {item["task_id"] for item in datasets}
    if used != set(ids):
        raise ValueError(f"tasks without training data: {sorted(set(ids) - used)}")
    return {"schema_version": 1, "tasks": normalized_tasks, "datasets": datasets}


def validate_dataset_sources(sources, ids):
    """Dataset records ``{task_id, label_file, video_file, advantage_file}`` with absolute paths resolved to
    existing files. ``ids`` is the task table; when it is empty the sources must not carry a ``task_id``.
    An optional ``advantage`` (true / false) labels every frame of that source with the same value instead
    of reading labels; it is kept in the record only when set.
    """
    if not isinstance(sources, list) or not sources:
        raise ValueError("datasets must be a nonempty list")
    seen_labels, seen_videos, datasets = set(), set(), []
    for item in sources:
        _check_keys(item, DATASET_KEYS, "datasets")
        if ids:
            if "task_id" not in item:
                raise ValueError(f"dataset requires task_id: {item}")
            task_id = task_id_value(item["task_id"])
            if task_id not in ids:
                raise ValueError(f"dataset references unknown task_id {task_id}")
        elif item.get("task_id") is not None:
            raise ValueError("dataset task_id requires a task table (tasks)")
        else:
            task_id = None
        record = {"task_id": task_id}
        for key in ("label_file", "video_file", "advantage_file"):
            value = item.get(key)
            if key == "advantage_file" and value is None:
                record[key] = None
                continue
            if not isinstance(value, str) or not value:
                raise ValueError(f"dataset requires {key}")
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                raise ValueError(f"{key} must be an absolute path: {value}")
            candidate = candidate.resolve(strict=True)
            if not candidate.is_file():
                raise ValueError(f"{key} is not a file: {candidate}")
            record[key] = str(candidate)
        if item.get("advantage") is not None:
            if not isinstance(item["advantage"], bool):
                raise ValueError(f"dataset advantage must be true / false, got {item['advantage']!r}")
            if record["advantage_file"] is not None:
                raise ValueError("dataset advantage and advantage_file are mutually exclusive")
            record["advantage"] = item["advantage"]
        if record["label_file"] in seen_labels or record["video_file"] in seen_videos:
            raise ValueError("duplicate dataset source; each label and video must be assigned once")
        seen_labels.add(record["label_file"])
        seen_videos.add(record["video_file"])
        datasets.append(record)
    return datasets
