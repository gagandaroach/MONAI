# Copyright 2020 - 2021 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Utilities for accessing Nvidia MMARs

See Also:
    - https://docs.nvidia.com/clara/clara-train-sdk/pt/mmar.html
"""

import json
import os
import warnings
from typing import Mapping

import torch

import monai.networks.nets as monai_nets
from monai.apps.utils import download_and_extract
from monai.utils.module import optional_import

from .model_desc import MODEL_DESC
from .model_desc import RemoteMMARKeys as Keys

__all__ = ["download_mmar", "load_from_mmar"]


def _get_model_spec(idx):
    """get model specification by `idx`. `idx` could be index of the constant tuple of dict or the actual model ID."""
    if isinstance(idx, int):
        return MODEL_DESC[idx]
    if isinstance(idx, str):
        key = idx.strip().lower()
        for cand in MODEL_DESC:
            if cand[Keys.ID].strip().lower() == key:
                return cand
    print(f"Available specs are: {MODEL_DESC}.")
    raise ValueError(f"Unknown MODEL_DESC request: {idx}")


def download_mmar(item, mmar_dir=None, progress: bool = True):
    """
    Download and extract Medical Model Archive (MMAR) from Nvidia Clara Train.

    See Also:
        - https://docs.nvidia.com/clara/
        - Nvidia NGC Registry CLI
        - https://docs.nvidia.com/clara/clara-train-sdk/pt/mmar.html

    Args:
        item: the corresponding model item from `MODEL_DESC`.
        mmar_dir: target directory to store the MMAR, default is mmars subfolder under `torch.hub get_dir()`.
        progress: whether to display a progress bar.

    Examples::
        >>> from monai.apps import download_mmar
        >>> download_mmar("clara_pt_prostate_mri_segmentation_1", mmar_dir=".")

    Returns:
        The local directory of the downloaded model.
    """
    if not isinstance(item, Mapping):
        item = _get_model_spec(item)
    if not mmar_dir:
        get_dir, has_home = optional_import("torch.hub", name="get_dir")
        if has_home:
            mmar_dir = os.path.join(get_dir(), "mmars")
        else:
            raise ValueError("mmar_dir=None, but no suitable default directory computed. Upgrade Pytorch to 1.6+ ?")

    model_dir = os.path.join(mmar_dir, item[Keys.ID])
    download_and_extract(
        url=item[Keys.URL],
        filepath=os.path.join(mmar_dir, f"{item[Keys.ID]}.{item[Keys.FILE_TYPE]}"),
        output_dir=model_dir,
        hash_val=item[Keys.HASH_VAL],
        hash_type=item[Keys.HASH_TYPE],
        file_type=item[Keys.FILE_TYPE],
        has_base=False,
        progress=progress,
    )
    return model_dir


def load_from_mmar(
    item,
    mmar_dir=None,
    progress: bool = True,
    map_location=None,
    pretrained=True,
    weights_only=False,
    model_key: str = "model",
):
    """
    Download and extract Medical Model Archive (MMAR) model weights from Nvidia Clara Train.

    Args:
        item: the corresponding model item from `MODEL_DESC`.
        mmar_dir: : target directory to store the MMAR, default is mmars subfolder under `torch.hub get_dir()`.
        progress: whether to display a progress bar when downloading the content.
        map_location: pytorch API parameter for `torch.load` or `torch.jit.load`.
        pretrained: whether to load the pretrained weights after initializing a network module.
        weights_only: whether to load only the weights instead of initializing the network module and assign weights.
        model_key: a key to search in the model file or config file for the model dictionary.
            Currently this function assumes that the model dictionary has
            `{"[name|path]": "test.module", "args": {'kw': 'test'}}`.

    Examples::
        >>> from monai.apps import load_from_mmar
        >>> unet_model = load_from_mmar("clara_pt_prostate_mri_segmentation_1", mmar_dir=".", map_location="cpu")
        >>> print(unet_model)

    See Also:
        https://docs.nvidia.com/clara/
    """
    if not isinstance(item, Mapping):
        item = _get_model_spec(item)
    model_dir = download_mmar(item=item, mmar_dir=mmar_dir, progress=progress)
    model_file = os.path.join(model_dir, item[Keys.MODEL_FILE])
    print(f'\n*** "{item[Keys.ID]}" available at {model_dir}.')

    # loading with `torch.jit.load`
    if f"{model_file}".endswith(".ts"):
        if not pretrained:
            warnings.warn("Loading a ScriptModule, 'pretrained' option ignored.")
        if weights_only:
            warnings.warn("Loading a ScriptModule, 'weights_only' option ignored.")
        return torch.jit.load(model_file, map_location=map_location)

    # loading with `torch.load`
    model_dict = torch.load(model_file, map_location=map_location)
    if weights_only:
        return model_dict.get(model_key, model_dict)  # model_dict[model_key] or model_dict directly

    # 1. search `model_dict['train_config]` for model config spec.
    model_config = _get_val(dict(model_dict).get("train_conf", {}), key=model_key, default={})
    if not model_config:
        # 2. search json CONFIG_FILE for model config spec.
        json_path = os.path.join(model_dir, item.get(Keys.CONFIG_FILE, "config_train.json"))
        with open(json_path) as f:
            conf_dict = json.load(f)
        conf_dict = dict(conf_dict)
        model_config = _get_val(conf_dict, key=model_key, default={})
    if not model_config:
        # 3. search `model_dict` for model config spec.
        model_config = _get_val(dict(model_dict), key=model_key, default={})

    if not (model_config and isinstance(model_config, Mapping)):
        raise ValueError(
            f"Could not load model config dictionary from config: {item.get(Keys.CONFIG_FILE)}, "
            f"or from model file: {item.get(Keys.MODEL_FILE)}."
        )

    # parse `model_config` for model class and model parameters
    if model_config.get("name"):  # model config section is a "name"
        model_name = model_config["name"]
        model_cls = monai_nets.__dict__[model_name]
    elif model_config.get("path"):  # model config section is a "path"
        # https://docs.nvidia.com/clara/clara-train-sdk/pt/byom.html
        model_module, model_name = model_config.get("path", ".").rsplit(".", 1)
        model_cls, has_cls = optional_import(module=model_module, name=model_name)
        if not has_cls:
            raise ValueError(
                f"Could not load MMAR model config {model_config.get('path', '')}, "
                f"Please make sure MMAR's sub-folders in '{model_dir}' is on the PYTHONPATH."
                "See also: https://docs.nvidia.com/clara/clara-train-sdk/pt/byom.html"
            )
    else:
        raise ValueError(f"Could not load model config {model_config}.")

    print(f"*** Model: {model_cls}")
    model_kwargs = model_config.get("args", None)
    if model_kwargs:
        model_inst = model_cls(**model_kwargs)
        print(f"*** Model params: {model_kwargs}")
    else:
        model_inst = model_cls()
    if pretrained:
        model_inst.load_state_dict(model_dict.get(model_key, model_dict))
    print("\n---")
    print(f"For more information, please visit {item[Keys.DOC]}\n")
    return model_inst


def _get_val(input_dict: Mapping, key="model", default=None):
    """
    Search for the item with `key` in `config_dict`.
    Returns: the first occurrence of `key` in a breadth first search.
    """
    if key in input_dict:
        return input_dict[key]
    for sub_dict in input_dict:
        val = input_dict[sub_dict]
        if isinstance(val, Mapping):
            found_val = _get_val(val, key=key, default=None)
            if found_val is not None:
                return found_val
    return default
