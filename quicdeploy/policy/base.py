"""Policy adapter contract and loader.
策略适配器接口与加载器。

An adapter turns synchronized camera images into a physical EEF20 chunk.
适配器负责把同步后的相机图像转换为物理量纲的 EEF20 动作块。

    observation = {
        "images":    {camera_name: uint8 RGB HxWx3},
        "eef_state": EEF20 anchor (measured FK at the observation),
        "timestamp": head-camera time, ...
    }
    context = {
        "seed", "episode", "anchor_index",     # deterministic noise / 确定性噪声
        "rng",                                 # numpy Generator
        "prefix_absolute": [k,20] | None,      # RTC prefix, absolute poses / RTC 绝对前缀
    }
    infer(...) -> [H, 20] relative EEF20 (decoded later against the anchor)
              -> [H, 20] 相对位姿（之后由调度器用锚点解码）

New model families: subclass PolicyAdapter and reference it as
"package.module:ClassName" in the model JSON "adapter" field.
新增模型：继承 PolicyAdapter，并在模型 JSON 的 adapter 字段写 "模块:类名"。
"""
import importlib


class PolicyAdapter:
    def load(self, config):
        raise NotImplementedError

    def reset(self):
        """Called once after load. / 加载后调用一次。"""

    def infer(self, observation, context):
        raise NotImplementedError


def load_adapter(config):
    module, name = config["adapter"].split(":")
    adapter = getattr(importlib.import_module(module), name)()
    if not isinstance(adapter, PolicyAdapter):
        raise TypeError(f"{config['adapter']} is not a PolicyAdapter")
    adapter.load(config)
    adapter.reset()
    return adapter
