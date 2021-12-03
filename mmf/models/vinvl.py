# Copyright (c) Facebook, Inc. and its affiliates.

# Code based off https://github.com/microsoft/Oscar
# modified for MMF
# Licensed under the MIT license.

import logging
from collections import namedtuple
from typing import Dict, Optional, Tuple

import torch
from mmf.common.sample import SampleList
from mmf.models.transformers.heads.mlm import MLM
from mmf.models.transformers.heads.mlp import MLP
from mmf.utils.general import retry_n
from torch import Tensor, nn
from transformers.modeling_bert import (
    BertConfig,
    BertEmbeddings,
    BertEncoder,
    BertPreTrainedModel,
)

logger = logging.getLogger(__name__)

NUM_RETRIES = 6


class VinVLBase(BertPreTrainedModel):
    """VinVL Bert Encoder for image features
    From https://github.com/microsoft/Oscar/blob/master/oscar/modeling/modeling_bert.py
    Is a thin wrapper around BertEncoder that handles image features
    """

    def __init__(self, config: BertConfig):
        super().__init__(config)
        self.embeddings = BertEmbeddings(config)
        self.encoder = BertEncoder(config)

        self.img_dim = config.img_feature_dim
        self.use_img_layernorm = getattr(config, "use_img_layernorm", False)

        img_projection = nn.Linear(self.img_dim, self.config.hidden_size, bias=True)
        img_embedding_list = [img_projection]
        if self.use_img_layernorm:
            img_embedding_list += [
                nn.LayerNorm(config.hidden_size, eps=config.img_layer_norm_eps)
            ]
        dropout = nn.Dropout(config.hidden_dropout_prob)
        img_embedding_list += [dropout]
        # is an image encoding used as input to the transformer trunk
        self.img_embedding = nn.Sequential(*img_embedding_list)

    def forward(
        self,
        input_ids: Tensor,
        img_feats: Tensor,
        token_type_ids: Optional[Tensor] = None,
        attention_mask: Optional[Tensor] = None,
        position_ids: Optional[Tensor] = None,
    ) -> Tuple[Tensor]:
        if attention_mask is None:
            attention_mask = torch.ones(
                (input_ids.size(0), input_ids.size(1) + img_feats.size(1))
            ).to(input_ids.device)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)
        # We can provide a self-attention mask of dimensions
        # [batch_size, from_seq_length, to_seq_length]
        # ourselves in which case we just need to make it broadcastable to all heads.
        # attention_mask with dim 3 is to specify a unique mask for each feature,
        # it is broadcast over heads.
        if attention_mask.dim() == 3:
            extended_attention_mask = attention_mask[:, None, :, :]
        elif attention_mask.dim() == 2:
            # Provided a padding mask of dimensions [batch_size, seq_length]
            # Make the mask broadcastable to
            # [batch_size, num_heads, seq_length, seq_length]
            extended_attention_mask = attention_mask[:, None, None, :]
        else:
            raise ValueError(
                f"Wrong shape for input_ids (shape {input_ids.shape})"
                + " or attention_mask (shape {attention_mask.shape})"
            )

        # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
        # masked positions, this operation will create a tensor which is 0.0 for
        # positions we want to attend and -10000.0 for masked positions.
        # Since we are adding it to the raw scores before the softmax, this is
        # effectively the same as removing these entirely.
        extended_attention_mask = extended_attention_mask.to(dtype=self.dtype)
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

        # Do embeddings
        text_embedding_output = self.embeddings(
            input_ids, position_ids=position_ids, token_type_ids=token_type_ids
        )
        img_embedding_output = self.img_embedding(img_feats)
        embedding_output = torch.cat((text_embedding_output, img_embedding_output), 1)

        encoder_outputs = self.encoder(
            embedding_output,
            extended_attention_mask,
            output_hidden_states=True,
        )
        layers = namedtuple("TransformerOutput", ["last_hidden_state", "hidden_layers"])
        return layers(encoder_outputs[0], encoder_outputs[1])


def build_vinvl_base(
    bert_model_name: str = "bert-base-uncased",
    img_feature_dim: int = 2054,
    use_img_layernorm: bool = True,
    img_layer_norm_eps: float = 1e-12,
    random_init: bool = True,
) -> VinVLBase:
    bert_config = retry_n(
        NUM_RETRIES,
        BertConfig.from_pretrained,
        bert_model_name,
    )
    # augment hf BertConfig for vinvl BertImgModel config
    bert_config.img_feature_dim = img_feature_dim
    bert_config.use_img_layernorm = use_img_layernorm
    bert_config.img_layer_norm_eps = img_layer_norm_eps

    if random_init:
        bert = VinVLBase(bert_config)
    else:
        bert = retry_n(
            NUM_RETRIES,
            VinVLBase.from_pretrained,
            bert_model_name,
            config=bert_config,
        )
    return bert


class VinVLForClassification(nn.Module):
    """VINVL wrapper for classification"""

    def __init__(
        self,
        mlp_config: Optional[Dict] = None,
        loss_config: Optional[Dict] = None,
        random_init: bool = False,
        bert_model_name: str = "bert-base-uncased",
        img_feature_dim: int = 2054,
        use_img_layernorm: bool = True,
        img_layer_norm_eps: float = 1e-12,
        *args,
        **kwargs,
    ):
        """VinVL model constructor for classification.
        MLP head is configurable through Dict type.
        Consult the MLP head class for the config options.

        Args:
            mlp_config (Optional[Dict], optional):
                Classifier MLP head config.
                Defaults to {"num_layers": 0}.
            loss_config (Optional[Dict], optional):
                nn.CrossEntropyLoss params dict.
                Defaults to {}.
            random_init (bool, optional):
                Flag to load VinVL bert weights from random_init.
                Defaults to False.
            bert_model_name (str, optional):
                Name for base bert model.
                Used for VinVL base configs and weights.
                Defaults to "bert-base-uncased".
            img_feature_dim (int, optional):
                The size of the VinVL image feature inputs.
                Defaults to 2054.
            use_img_layernorm (bool, optional):
                Flag to use layernorm on image encoding.
                Defaults to True.
            img_layer_norm_eps (float, optional):
                Image layernorm epsilon. Defaults to 1e-12.
        """
        super().__init__()
        if mlp_config is None:
            mlp_config = {"num_layers": 0}
        if loss_config is None:
            loss_config = {}

        self.bert = build_vinvl_base(
            bert_model_name=bert_model_name,
            img_feature_dim=img_feature_dim,
            use_img_layernorm=use_img_layernorm,
            img_layer_norm_eps=img_layer_norm_eps,
            random_init=random_init,
        )
        self.classifier = MLP(config=mlp_config)
        self.ce_loss = nn.CrossEntropyLoss(**loss_config)

    def forward(
        self,
        input_ids: Tensor,
        token_type_ids: Tensor,
        attention_mask: Tensor,
        img_feats: Tensor,
        position_ids: Optional[Tensor] = None,
        labels: Optional[Tensor] = None,
    ) -> Dict[str, Tensor]:
        sequence_output = self.bert(
            input_ids,
            img_feats=img_feats,
            position_ids=position_ids,
            token_type_ids=token_type_ids,
            attention_mask=attention_mask,
        ).last_hidden_state
        logits = self.classifier(sequence_output)["scores"]
        result = {"scores": logits}

        if labels is not None:
            ce_loss = self.ce_loss(logits.view(-1, logits.size(1)), labels.view(-1))
            result["losses"] = {"ce": ce_loss}
        return result


class VinVLForPretraining(nn.Module):
    """VINVL wrapper for pretraining
    MLM loss is described in https://arxiv.org/pdf/2004.06165.pdf
    Contrastive loss is an itm loss to guess,
        0 for a match,
        1 for a corrupt caption,
        2 for corrupt image labels
    VinVL trains with object detection labels concatenated with the input text.
    """

    def __init__(
        self,
        mlm_config: Optional[Dict] = None,
        contrast_config: Optional[Dict] = None,
        random_init: bool = False,
        bert_model_name: str = "bert-base-uncased",
        img_feature_dim: int = 2054,
        use_img_layernorm: bool = True,
        img_layer_norm_eps: float = 1e-12,
        *args,
        **kwargs,
    ):
        """VinVL model constructor for pretraining.
        MLM and Contrastive Loss heads are configurable through Dict types.
        Consult MLM and MLP head classes for their config options.

        Args:
            mlm_config (Optional[Dict], optional):
                Config object for MLM head.
                Defaults to {} which uses the default MLM configs.
            contrast_config (Optional[Dict], optional):
                Config object for MLP head for 3-way contrastive loss.
                Defaults to {"num_layers": 0, "num_labels": 3}
            random_init (bool, optional):
                Flag to load VinVL bert weights from random_init.
                Defaults to False.
            bert_model_name (str, optional):
                Name for base bert model.
                Used for VinVL base configs and weights.
                Defaults to "bert-base-uncased".
            img_feature_dim (int, optional):
                The size of the VinVL image feature inputs.
                Defaults to 2054.
            use_img_layernorm (bool, optional):
                Flag to use layernorm on image encoding.
                Defaults to True.
            img_layer_norm_eps (float, optional):
                Image layernorm epsilon. Defaults to 1e-12.
        """
        super().__init__()
        if mlm_config is None:
            mlm_config = {}
        if contrast_config is None:
            contrast_config = {"num_layers": 0, "num_labels": 3}

        self.bert = build_vinvl_base(
            bert_model_name=bert_model_name,
            img_feature_dim=img_feature_dim,
            use_img_layernorm=use_img_layernorm,
            img_layer_norm_eps=img_layer_norm_eps,
            random_init=random_init,
        )
        self.mlm_head = MLM(config=mlm_config)
        self.ce_loss = nn.CrossEntropyLoss()
        self.contrast_head = MLP(config=contrast_config)

    def mlm_forward(
        self,
        input_ids_masked: Tensor,
        lm_label_ids: Tensor,
        token_type_ids: Tensor,
        attention_mask: Tensor,
        img_feats: Tensor,
        position_ids: Optional[Tensor] = None,
    ) -> Dict[str, Tensor]:

        hidden_layers = self.bert(
            input_ids_masked,
            img_feats=img_feats,
            position_ids=position_ids,
            token_type_ids=token_type_ids,
            attention_mask=attention_mask,
        ).last_hidden_state

        mlm_labels = {}
        mlm_labels["text"] = lm_label_ids
        mlm_labels["image"] = torch.full(
            img_feats.shape[:2],
            fill_value=-1,
            dtype=torch.long,
            device=lm_label_ids.device,
        )
        mlm_labels["combined_labels"] = torch.cat(
            [mlm_labels["text"], mlm_labels["image"]], dim=-1
        )

        processed_sample_list = SampleList({"mlm_labels": mlm_labels})
        return self.mlm_head(
            hidden_layers, processed_sample_list=processed_sample_list
        )["losses"]

    def contrastive_forward(
        self,
        input_ids: Tensor,
        token_type_ids: Tensor,
        attention_mask: Tensor,
        img_feats: Tensor,
        contrastive_labels: Tensor,
        position_ids: Optional[Tensor] = None,
    ) -> Dict[str, Tensor]:

        last_hidden_state = self.bert(
            input_ids,
            img_feats=img_feats,
            position_ids=position_ids,
            token_type_ids=token_type_ids,
            attention_mask=attention_mask,
        ).last_hidden_state
        logits = self.contrast_head(last_hidden_state)["scores"]
        # contrastive 3-way loss has 3 classes,
        # 0 for a match, 1, 2 for a corrupt caption/image
        # labels respectively
        loss = self.ce_loss(
            logits.contiguous().view(-1, 3),
            contrastive_labels.contiguous().view(-1),
        )
        return {"vinvl_three_way_contrastive_loss": loss}

    def forward(
        self,
        input_ids_masked: Tensor,
        input_ids_corrupt: Tensor,
        lm_label_ids: Tensor,
        contrastive_labels: Tensor,
        token_type_ids: Tensor,
        attention_mask: Tensor,
        token_type_ids_corrupt: Tensor,
        attention_mask_corrupt: Tensor,
        img_feats: Tensor,
        position_ids: Optional[Tensor] = None,
    ) -> Dict[str, Tensor]:

        mlm_result = self.mlm_forward(
            input_ids_masked,
            lm_label_ids,
            token_type_ids,
            attention_mask,
            img_feats,
            position_ids,
        )

        contrastive_loss_result = self.contrastive_forward(
            input_ids_corrupt,
            token_type_ids_corrupt,
            attention_mask_corrupt,
            img_feats,
            contrastive_labels,
            position_ids,
        )
        losses = {**mlm_result, **contrastive_loss_result}
        return {"losses": losses}
