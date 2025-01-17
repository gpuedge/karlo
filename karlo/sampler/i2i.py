# ------------------------------------------------------------------------------------
# Karlo-v1.0.alpha
# Copyright (c) 2022 KakaoBrain. All Rights Reserved.
# ------------------------------------------------------------------------------------

from typing import Iterator

import torch
import torchvision.transforms.functional as TVF
from torchvision.transforms import InterpolationMode

from .template import BaseSampler, CKPT_PATH


class I2ISampler(BaseSampler):
    def __init__(
        self,
        root_dir: str,
        sampling_type: str = "default",
    ):
        super().__init__(root_dir, sampling_type)

    @classmethod
    def from_pretrained(
        cls,
        root_dir: str,
        clip_model_path: str,
        clip_stat_path: str,
        sampling_type: str = "default",
    ):

        model = cls(
            root_dir=root_dir,
            sampling_type=sampling_type,
        )
        model.load_clip(clip_model_path)
        model.load_decoder(f"{CKPT_PATH['decoder']}")
        model.load_sr_64_256(CKPT_PATH["sr_256"])

        return model

    def preprocess(
        self,
        image,
        prompt: str,
        bsz: int,
    ):
        prompts_batch = [prompt for _ in range(bsz)]
        decoder_cf_scales_batch = [self._decoder_cf_scale] * len(prompts_batch)
        decoder_cf_scales_batch = torch.tensor(decoder_cf_scales_batch, device="cuda")

        # preprocess input image
        image = TVF.normalize(
            TVF.to_tensor(
                TVF.resize(
                    image,
                    [224, 224],
                    interpolation=InterpolationMode.BICUBIC,
                    antialias=True,
                )
            ),
            mean=[0.48145466, 0.4578275, 0.40821073],
            std=[0.26862954, 0.26130258, 0.27577711],
        ).unsqueeze(0)
        image_batch = image.repeat(bsz, 1, 1, 1).cuda()

        """ Get CLIP text and image features """
        clip_model = self._clip
        tokenizer = self._tokenizer
        max_txt_length = 77

        tok, mask = tokenizer.padded_tokens_and_mask(prompts_batch, max_txt_length)
        cf_token, cf_mask = tokenizer.padded_tokens_and_mask([""], max_txt_length)
        if not (cf_token.shape == tok.shape):
            cf_token = cf_token.expand(tok.shape[0], -1)
            cf_mask = cf_mask.expand(tok.shape[0], -1)

        tok = torch.cat([tok, cf_token], dim=0)
        mask = torch.cat([mask, cf_mask], dim=0)

        tok, mask = tok.to(device="cuda"), mask.to(device="cuda")
        txt_feat, txt_feat_seq = clip_model.encode_text(tok)
        img_feat = clip_model.encode_image(image_batch)

        return (
            prompts_batch,
            decoder_cf_scales_batch,
            txt_feat,
            txt_feat_seq,
            tok,
            mask,
            img_feat,
        )

    def __call__(
        self,
        image,
        bsz: int,
        progressive_mode=None,
    ) -> Iterator[torch.Tensor]:
        assert progressive_mode in ("loop", "stage", "final")
        with torch.no_grad(), torch.cuda.amp.autocast():
            (
                prompts_batch,
                decoder_cf_scales_batch,
                txt_feat,
                txt_feat_seq,
                tok,
                mask,
                img_feat,
            ) = self.preprocess(
                image=image,
                prompt="",
                bsz=bsz,
            )

            """ Generate 64x64px images """
            images_64_outputs = self._decoder(
                txt_feat,
                txt_feat_seq,
                tok,
                mask,
                img_feat,
                cf_guidance_scales=decoder_cf_scales_batch,
                timestep_respacing=self._decoder_sm,
            )

            images_64 = None
            for k, out in enumerate(images_64_outputs):
                images_64 = out
                if progressive_mode == "loop":
                    yield torch.clamp(out * 0.5 + 0.5, 0.0, 1.0)
            if progressive_mode == "stage":
                yield torch.clamp(out * 0.5 + 0.5, 0.0, 1.0)

            images_64 = torch.clamp(images_64, -1, 1)

            """ Upsample 64x64 to 256x256 """
            images_256 = TVF.resize(
                images_64,
                [256, 256],
                interpolation=InterpolationMode.BICUBIC,
                antialias=True,
            )
            images_256_outputs = self._sr_64_256(
                images_256, timestep_respacing=self._sr_sm
            )

            for k, out in enumerate(images_256_outputs):
                images_256 = out
                if progressive_mode == "loop":
                    yield torch.clamp(out * 0.5 + 0.5, 0.0, 1.0)
            if progressive_mode == "stage":
                yield torch.clamp(out * 0.5 + 0.5, 0.0, 1.0)

        yield torch.clamp(images_256 * 0.5 + 0.5, 0.0, 1.0)
