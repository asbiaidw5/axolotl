"""Implements the ReLoRA training procedure from https://arxiv.org/abs/2307.05695, minus the initial full fine-tune."""
import glob
import json
import logging
import os.path
import shutil
from pathlib import Path
from typing import Dict, List, Sequence

import bitsandbytes as bnb
import peft
import safetensors.torch as st
import torch
from huggingface_hub import snapshot_download
from torch.optim.lr_scheduler import LRScheduler
from torch.optim.optimizer import Optimizer
from transformers import (
    TrainerCallback,
    TrainerControl,
    TrainerState,
    TrainingArguments,
)
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR

from axolotl.utils.dict import DictDefault
from axolotl.utils.distributed import is_main_process

LOG = logging.getLogger("axolotl.relora")


def reset_optimizer(optimizer: torch.optim.Optimizer):
    for group in optimizer.param_groups:
        for param in group["params"]:
            param_state = optimizer.state[param]
            for key in param_state:
                if "qmap" in key:
                    continue

                if key == "step" and isinstance(param_state[key], int):
                    param_state[key] = 0
                else:
                    param_state[key] = torch.zeros_like(param_state[key])


class ReLoRACallback(TrainerCallback):
    """Callback to merge LoRA weights into the base model and save full-weight checkpoints"""

    def __init__(self, cfg: DictDefault):
        self.relora_steps = cfg.relora_steps
        self.cpu_offload = cfg.relora_cpu_offload
        self.quantized = cfg.load_in_4bit or cfg.load_in_8bit
        self.last_full_model = cfg.base_model
        self.resume_from_checkpoint = cfg.resume_from_checkpoint

        if not os.path.exists(self.last_full_model):
            self.last_full_model = str(Path(snapshot_download(cfg.base_model)))

        assert os.path.exists(
            self.last_full_model
        ), "for ReLORA base_model must be a local path"

        self.num_lora_restarts = 0
        self.need_full_save = False

    def on_train_begin(
        self,
        _args: TrainingArguments,
        _state: TrainerState,
        control: TrainerControl,
        model: peft.LoraModel,
        **_kwargs,
    ):
        if self.resume_from_checkpoint:
            weight_path = os.path.join(self.resume_from_checkpoint, "relora")
            if not os.path.exists(weight_path):
                LOG.warning(
                    "Resuming ReLoRA from checkpoint, but no full-weight save found"
                )
            else:
                LOG.info(f"Loading adjusted base weights from {weight_path}")
                load_weight_checkpoint(model, weight_path)
        return control

    def on_step_begin(
        self,
        args: TrainingArguments,
        state: TrainerState,
        control: TrainerControl,
        model: peft.LoraModel,
        optimizer: torch.optim.Optimizer,
        **_kwargs,
    ):
        if state.global_step > 0 and state.global_step % self.relora_steps == 0:
            checkpoint_folder = os.path.join(
                args.output_dir,
                f"{PREFIX_CHECKPOINT_DIR}-{state.global_step}",
                "relora",
            )

            with torch.no_grad():
                merge_and_save(
                    model,
                    self.last_full_model,
                    checkpoint_folder,
                    reinit=True,
                    quantized=self.quantized,
                    actually_save=is_main_process(),
                    cpu_offload=self.cpu_offload,
                )
                reset_optimizer(optimizer)

            if self.quantized:
                self.last_full_model = checkpoint_folder
            self.num_lora_restarts += 1

        return control

    def on_save(
        self,
        args: TrainingArguments,
        state: TrainerState,
        control: TrainerControl,
        model: peft.LoraModel,
        **_kwargs,
    ):
        checkpoint_folder = os.path.join(
            args.output_dir, f"{PREFIX_CHECKPOINT_DIR}-{state.global_step}", "relora"
        )
        if (
            state.global_step >= self.relora_steps
            and state.global_step % self.relora_steps != 0
        ):
            if self.quantized:
                if self.last_full_model != checkpoint_folder:
                    # ensure the latest full parameter save is in the latest checkpoint
                    # folder, so that automatic pruning of checkpoints does not remove it
                    LOG.info(f"moving last full parameter save to {checkpoint_folder}")
                    os.makedirs(checkpoint_folder, exist_ok=True)
                    chunks = glob.glob(
                        f"{self.last_full_model}/model*.safetensors"
                    ) + glob.glob(f"{self.last_full_model}/model*.index.json")
                    for path in chunks:
                        new_path = os.path.abspath(shutil.move(path, checkpoint_folder))
                        try:
                            os.symlink(new_path, path)
                        except OSError:
                            # probably on windows without permission to symlink
                            pass

                    self.last_full_model = checkpoint_folder
            else:
                model.model.save_pretrained(checkpoint_folder, safe_serialization=True)

        return control

    def on_log(
        self,
        _args: TrainingArguments,
        _state: TrainerState,
        control: TrainerControl,
        logs: Dict[str, float],
        **_kwargs,
    ):
        logs["num_lora_restarts"] = self.num_lora_restarts
        return control

    def on_train_end(
        self,
        args: TrainingArguments,
        _state: TrainerState,
        control: TrainerControl,
        model: peft.LoraModel,
        **_kwargs,
    ):
        if self.quantized:
            # perform final merge and save
            with torch.no_grad():
                merge_and_save(
                    model,
                    self.last_full_model,
                    args.output_dir,
                    reinit=False,
                    quantized=self.quantized,
                    actually_save=is_main_process(),
                    cpu_offload=self.cpu_offload,
                )
        # no need to save if unquantized, as finetune.py will call merge_and_unload()
        return control


class ReLoRAScheduler(LRScheduler):
    """Wraps another scheduler to apply per-lora-restart learning rate warmups."""

    def __init__(
        self,
        optimizer: Optimizer,
        inner_schedule: LRScheduler,
        relora_steps: int,
        warmup_steps: int,
        min_lr_scale: float = 0.001,
    ) -> None:
        self.inner_schedule = inner_schedule
        self.relora_steps = relora_steps
        self.warmup_steps = warmup_steps
        self.min_lr_scale = min_lr_scale
        super().__init__(optimizer, inner_schedule.last_epoch, inner_schedule.verbose)

    def get_lr(self) -> float:
        self.inner_schedule.last_epoch = self.last_epoch

        original = self.inner_schedule.get_lr()
        step = self.last_epoch
        if step < self.relora_steps:
            scale = 1
        else:
            cycle_t = min(1.0, (step % self.relora_steps) / self.warmup_steps)
            scale = cycle_t * (1 - self.min_lr_scale) + self.min_lr_scale

        if isinstance(original, Sequence):
            return [lr * scale for lr in original]
        return original * scale


def sharded_paths(path: str, module_names: List[str]) -> Dict[str, str]:
    model_name = "model.safetensors"
    if not os.path.exists(str(Path(path) / model_name)) and not os.path.exists(
        str(Path(path) / f"{model_name}.index.json")
    ):
        model_name = "pytorch_model.bin"

    index_path = str(Path(path) / f"{model_name}.index.json")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data["weight_map"]
    return {(module_name + ".weight"): model_name for module_name in module_names}


def lora_delta_weight(layer: peft.tuners.lora.LoraLayer, device) -> torch.Tensor:
    if isinstance(layer, (peft.tuners.lora.Linear8bitLt, peft.tuners.lora.Linear4bit)):
        adapter = layer.active_adapter
        return (
            peft.utils.transpose(
                layer.lora_B[adapter].weight.detach().to(device)
                @ layer.lora_A[adapter].weight.detach().to(device),
                getattr(layer, "fan_in_fan_out", False),
            )
            * layer.scaling[adapter]
        )

    return layer.get_delta_weight().to(device)


def find_lora_modules(model: peft.LoraModel) -> Dict[str, peft.tuners.lora.LoraLayer]:
    modules: Dict[str, peft.tuners.lora.LoraLayer] = {}

    key_list = [key for key, _ in model.model.named_modules() if "lora" not in key]
    for key in key_list:
        try:
            # pylint: disable=protected-access
            _parent, target, _target_name = peft.utils._get_submodules(model.model, key)
        except AttributeError:
            continue

        if isinstance(target, peft.tuners.lora.LoraLayer):
            modules[key] = target

    return modules


def update_weights(
    target: peft.tuners.lora.LoraLayer, new_weight: torch.Tensor, reinit: bool, device
):
    if reinit:
        for adapter_name in target.lora_A:
            target.reset_lora_parameters(adapter_name)
        for adapter_name in target.lora_embedding_A:
            target.reset_lora_parameters(adapter_name)

    if isinstance(target, peft.tuners.lora.Linear4bit):
        # This could be faster, but the quantization of Linear4bit weights occurs
        # when the module is moved from cpu to gpu. Without meddling *too* deeply in
        # PEFT's innards or maintaining a duplicate of that codepath, this is good
        # enough for now.
        target.weight.quant_state = None
        target.weight.data = new_weight.cpu()
        target.to(device)
    elif isinstance(target, peft.tuners.lora.Linear8bitLt):
        target.weight = bnb.nn.Int8Params(new_weight, requires_grad=False).to(device)
    else:
        target.weight.data = new_weight.to(device)


def merge_and_save(
    model: peft.LoraModel,
    model_src: str,
    model_dst: str,
    reinit: bool = False,
    quantized: bool = False,
    cpu_offload: bool = False,
    actually_save: bool = True,
):
    modules = find_lora_modules(model)

    if not quantized:
        for module_name, target in modules.items():
            update = target.get_delta_weight(target.active_adapter).detach()
            target.weight.data += update

            if reinit:
                for adapter_name in target.lora_A:
                    target.reset_lora_parameters(adapter_name)
                for adapter_name in target.lora_embedding_A:
                    target.reset_lora_parameters(adapter_name)
        return

    os.makedirs(model_dst, exist_ok=True)
    shard_paths = sharded_paths(model_src, modules.keys())
    out_shard_paths = {}

    unique_shards = list(set(shard_paths.values()))
    for shard_path in unique_shards:
        out_tensors = {}
        if shard_path.endswith(".safetensors"):
            in_tensors = st.load_file(str(Path(model_src) / shard_path))
        else:
            in_tensors = torch.load(Path(model_src) / shard_path)
            if "state_dict" in in_tensors:
                in_tensors = in_tensors["state_dict"]

        for module_name, target in modules.items():
            key = module_name + ".weight"
            if key not in shard_paths or shard_paths[key] != shard_path:
                continue

            orig_weight = in_tensors[key]
            old_dev = target.weight.device
            math_dev = "cpu" if cpu_offload else old_dev

            delta_weight = lora_delta_weight(target, math_dev)
            new_weight = orig_weight.to(math_dev) + delta_weight
            del delta_weight

            if actually_save:
                out_tensors[key] = new_weight.half().cpu()

            update_weights(target, new_weight, reinit=reinit, device=old_dev)

        if actually_save:
            out_shard_name = shard_path
            if out_shard_name.startswith("pytorch_model"):
                out_shard_name = (
                    out_shard_name.replace("pytorch_model", "model").rstrip(".bin")
                    + ".safetensors"
                )

            for module_name in in_tensors:
                if module_name not in out_tensors:
                    out_tensors[module_name] = in_tensors[module_name].half()
                out_shard_paths[module_name] = out_shard_name

            shard_fn = str(Path(model_dst) / out_shard_name)
            LOG.info(f"saving tensors to {shard_fn}")
            st.save_file(out_tensors, shard_fn, metadata={"format": "pt"})

        del in_tensors
        del out_tensors
        torch.cuda.empty_cache()

    if actually_save and len(unique_shards) > 1:
        with open(
            str(Path(model_dst, "model.safetensors.index.json")), "w", encoding="utf-8"
        ) as file:
            json.dump({"metadata": {}, "weight_map": out_shard_paths}, file)


def load_weight_checkpoint(model: peft.LoraModel, checkpoint_path: str):
    modules = find_lora_modules(model)
    shard_paths = sharded_paths(checkpoint_path, modules.keys())
    unique_shards = list(set(shard_paths.values()))

    for shard_path in unique_shards:
        tensors = st.load_file(os.path.join(checkpoint_path, shard_path))

        for module_name, target in modules.items():
            key = module_name + ".weight"
            if key not in shard_paths or shard_paths[key] != shard_path:
                continue

            new_weight = tensors[key]
            update_weights(
                target, new_weight, reinit=False, device=target.weight.device
            )
