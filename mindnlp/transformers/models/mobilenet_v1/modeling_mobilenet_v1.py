# coding=utf-8
# Copyright 2022 Apple Inc. and The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""MindSpore MobileNetV1 model."""

from typing import Optional, Union

import mindspore
from mindnlp.core import nn, ops
from mindnlp.core.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss

from ....common.activations import ACT2FN
from ...modeling_outputs import BaseModelOutputWithPoolingAndNoAttention, ImageClassifierOutputWithNoAttention
from ...modeling_utils import PreTrainedModel
from ....utils import logging
from .configuration_mobilenet_v1 import MobileNetV1Config


logger = logging.get_logger(__name__)


# General docstring
_CONFIG_FOR_DOC = "MobileNetV1Config"

# Base docstring
_CHECKPOINT_FOR_DOC = "google/mobilenet_v1_1.0_224"
_EXPECTED_OUTPUT_SHAPE = [1, 1024, 7, 7]

# Image classification docstring
_IMAGE_CLASS_CHECKPOINT = "google/mobilenet_v1_1.0_224"
_IMAGE_CLASS_EXPECTED_OUTPUT = "tabby, tabby cat"


def apply_tf_padding(features: mindspore.Tensor, conv_layer: nn.Conv2d) -> mindspore.Tensor:
    """
    Apply TensorFlow-style "SAME" padding to a convolution layer. See the notes at:
    https://www.tensorflow.org/api_docs/python/tf/nn#notes_on_padding_2
    """
    in_height, in_width = features.shape[-2:]
    stride_height, stride_width = conv_layer.stride
    kernel_height, kernel_width = conv_layer.kernel_size

    if in_height % stride_height == 0:
        pad_along_height = max(kernel_height - stride_height, 0)
    else:
        pad_along_height = max(kernel_height - (in_height % stride_height), 0)

    if in_width % stride_width == 0:
        pad_along_width = max(kernel_width - stride_width, 0)
    else:
        pad_along_width = max(kernel_width - (in_width % stride_width), 0)

    pad_left = pad_along_width // 2
    pad_right = pad_along_width - pad_left
    pad_top = pad_along_height // 2
    pad_bottom = pad_along_height - pad_top

    padding = (pad_left, pad_right, pad_top, pad_bottom)
    return nn.functional.pad(features, padding, "constant", 0.0)


class MobileNetV1ConvLayer(nn.Module):
    def __init__(
        self,
        config: MobileNetV1Config,
        in_channels: int,
        out_channels: int,
        kernel_size: int,
        stride: Optional[int] = 1,
        groups: Optional[int] = 1,
        bias: bool = False,
        use_normalization: Optional[bool] = True,
        use_activation: Optional[bool or str] = True,
    ) -> None:
        super().__init__()
        self.config = config

        if in_channels % groups != 0:
            raise ValueError(f"Input channels ({in_channels}) are not divisible by {groups} groups.")
        if out_channels % groups != 0:
            raise ValueError(f"Output channels ({out_channels}) are not divisible by {groups} groups.")

        padding = 0 if config.tf_padding else int((kernel_size - 1) / 2)

        self.convolution = nn.Conv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            groups=groups,
            bias=bias,
            padding_mode="zeros",
        )

        if use_normalization:
            self.normalization = nn.BatchNorm2d(
                num_features=out_channels,
                eps=config.layer_norm_eps,
                momentum=0.9997,
                affine=True,
                track_running_stats=True,
            )
        else:
            self.normalization = None

        if use_activation:
            if isinstance(use_activation, str):
                self.activation = ACT2FN[use_activation]
            elif isinstance(config.hidden_act, str):
                self.activation = ACT2FN[config.hidden_act]
            else:
                self.activation = config.hidden_act
        else:
            self.activation = None

    def forward(self, features: mindspore.Tensor) -> mindspore.Tensor:
        if self.config.tf_padding:
            features = apply_tf_padding(features, self.convolution)
        features = self.convolution(features)
        if self.normalization is not None:
            features = self.normalization(features)
        if self.activation is not None:
            features = self.activation(features)
        return features


class MobileNetV1PreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = MobileNetV1Config
    base_model_prefix = "mobilenet_v1"
    main_input_name = "pixel_values"
    supports_gradient_checkpointing = False
    _no_split_modules = []

    def _init_weights(self, module: Union[nn.Linear, nn.Conv2d]) -> None:
        """Initialize the weights"""
        if isinstance(module, (nn.Linear, nn.Conv2d)):
            nn.init.normal_(module.weight, mean=0.0, std=self.config.initializer_range)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module, nn.BatchNorm2d):
            nn.init.zeros_(module.bias)
            nn.init.ones_(module.weight)


class MobileNetV1Model(MobileNetV1PreTrainedModel):
    def __init__(self, config: MobileNetV1Config, add_pooling_layer: bool = True):
        super().__init__(config)
        self.config = config

        depth = 32
        out_channels = max(int(depth * config.depth_multiplier), config.min_depth)

        self.conv_stem = MobileNetV1ConvLayer(
            config,
            in_channels=config.num_channels,
            out_channels=out_channels,
            kernel_size=3,
            stride=2,
        )

        strides = [1, 2, 1, 2, 1, 2, 1, 1, 1, 1, 1, 2, 1]

        self.layer = nn.ModuleList()
        for i in range(13):
            in_channels = out_channels

            if strides[i] == 2 or i == 0:
                depth *= 2
                out_channels = max(int(depth * config.depth_multiplier), config.min_depth)

            self.layer.append(
                MobileNetV1ConvLayer(
                    config,
                    in_channels=in_channels,
                    out_channels=in_channels,
                    kernel_size=3,
                    stride=strides[i],
                    groups=in_channels,
                )
            )

            self.layer.append(
                MobileNetV1ConvLayer(
                    config,
                    in_channels=in_channels,
                    out_channels=out_channels,
                    kernel_size=1,
                )
            )

        self.pooler = nn.AdaptiveAvgPool2d((1, 1)) if add_pooling_layer else None

        # Initialize weights and apply final processing
        self.post_init()

    def _prune_heads(self, heads_to_prune):
        raise NotImplementedError

    def forward(
        self,
        pixel_values: Optional[mindspore.Tensor] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[tuple, BaseModelOutputWithPoolingAndNoAttention]:
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if pixel_values is None:
            raise ValueError("You have to specify pixel_values")

        hidden_states = self.conv_stem(pixel_values)
        all_hidden_states = () if output_hidden_states else None

        for i, layer_module in enumerate(self.layer):
            hidden_states = layer_module(hidden_states)

            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

        last_hidden_state = hidden_states

        if self.pooler is not None:
            pooled_output = ops.flatten(self.pooler(last_hidden_state), start_dim=1)
        else:
            pooled_output = None

        if not return_dict:
            return tuple(v for v in [last_hidden_state, pooled_output, all_hidden_states] if v is not None)

        return BaseModelOutputWithPoolingAndNoAttention(
            last_hidden_state=last_hidden_state,
            pooler_output=pooled_output,
            hidden_states=all_hidden_states,
        )


class MobileNetV1ForImageClassification(MobileNetV1PreTrainedModel):
    def __init__(self, config: MobileNetV1Config) -> None:
        super().__init__(config)

        self.num_labels = config.num_labels
        self.mobilenet_v1 = MobileNetV1Model(config)

        last_hidden_size = self.mobilenet_v1.layer[-1].convolution.out_channels

        # Classifier head
        self.dropout = nn.Dropout(config.classifier_dropout_prob, inplace=True)
        self.classifier = nn.Linear(last_hidden_size, config.num_labels) if config.num_labels > 0 else nn.Identity()

        # Initialize weights and apply final processing
        self.post_init()

    def forward(
        self,
        pixel_values: Optional[mindspore.Tensor] = None,
        output_hidden_states: Optional[bool] = None,
        labels: Optional[mindspore.Tensor] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[tuple, ImageClassifierOutputWithNoAttention]:
        r"""
        labels (`mindspore.Tensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the image classification/regression loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels == 1` a regression loss is computed (Mean-Square loss). If
            `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.mobilenet_v1(pixel_values, output_hidden_states=output_hidden_states, return_dict=return_dict)

        pooled_output = outputs.pooler_output if return_dict else outputs[1]
        logits = self.classifier(self.dropout(pooled_output))

        loss = None
        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and labels.dtype in (mindspore.int64, mindspore.int32):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"

            if self.config.problem_type == "regression":
                loss_fct = MSELoss()
                if self.num_labels == 1:
                    loss = loss_fct(logits.squeeze(), labels.squeeze())
                else:
                    loss = loss_fct(logits, labels)
            elif self.config.problem_type == "single_label_classification":
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss_fct = BCEWithLogitsLoss()
                loss = loss_fct(logits, labels)

        if not return_dict:
            output = (logits,) + outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return ImageClassifierOutputWithNoAttention(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
        )

__all__ = [
    "MobileNetV1ForImageClassification",
    "MobileNetV1Model",
    "MobileNetV1PreTrainedModel"
]
