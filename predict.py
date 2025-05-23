# Prediction interface for Cog ⚙️
# https://github.com/replicate/cog/blob/main/docs/python.md

from cog import BasePredictor, Input, Path
import os
import re
import time
import torch
import json
import requests
import tempfile
import tarfile
import numpy as np
from types import MethodType
from huggingface_hub import hf_hub_download
from PIL import Image
from typing import List
from diffusers import StableDiffusionPipeline, StableDiffusionImg2ImgPipeline
from diffusers import PNDMScheduler, AutoencoderKL, UNet2DConditionModel
from transformers import CLIPTextModel, CLIPTokenizer
from torchvision import transforms
from weights import WeightsDownloadCache
from transformers import CLIPImageProcessor
from lora_loading_patch import load_lora_into_transformer
from diffusers.pipelines.stable_diffusion.safety_checker import (
    StableDiffusionSafetyChecker
)

def truncate_prompt(prompt, max_tokens=75):
    """
    Truncate the prompt to ensure it doesn't exceed CLIP's token limit.
    This is a simple approximation - in production you might want a more accurate token counter.
    """
    words = prompt.split()
    if len(words) <= max_tokens:
        return prompt
    
    return " ".join(words[:max_tokens])

def patch_unet_get_aug_embed(unet):
    import torch
    original_method = unet.get_aug_embed

    def patched_method(self, *args, **kwargs):
        if "added_cond_kwargs" not in kwargs or kwargs["added_cond_kwargs"] is None:
            kwargs["added_cond_kwargs"] = {}

        conds = kwargs["added_cond_kwargs"]

        if "text_embeds" not in conds:
            batch_size = kwargs["sample"].shape[0] if "sample" in kwargs else 1
            conds["text_embeds"] = torch.zeros((batch_size, 1280), device=self.device)

        if "time_ids" not in conds:
            batch_size = kwargs["sample"].shape[0] if "sample" in kwargs else 1
            conds["time_ids"] = torch.zeros((batch_size, 6), device=self.device, dtype=torch.long)

        return original_method(*args, **kwargs)

    unet.get_aug_embed = MethodType(patched_method, unet)

MAX_IMAGE_SIZE = 1440
MODEL_CACHE = "cyberrealistic-pony"
SAFETY_CACHE = "safety-cache"
FEATURE_EXTRACTOR = "/src/feature-extractor"
SAFETY_URL = "https://weights.replicate.delivery/default/sdxl/safety-1.0.tar"
MODEL_URL = "https://huggingface.co/tomparisbiz/CyberRachel/resolve/main/cyberrealisticPony_v8.safetensors"

model_path = hf_hub_download(
    repo_id="tomparisbiz/CyberRachel",
    filename="cyberrealisticPony_v8.safetensors"
)

ASPECT_RATIOS = {
    "1:1": (1024, 1024),
    "16:9": (1344, 768),
    "21:9": (1536, 640),
    "3:2": (1216, 832),
    "2:3": (832, 1216),
    "4:5": (896, 1088),
    "5:4": (1088, 896),
    "3:4": (896, 1152),
    "4:3": (1152, 896),
    "9:16": (768, 1344),
    "9:21": (640, 1536),
}

def download_weights(url, dest, file=False):
    start = time.time()
    print("downloading url:", url)
    print("downloading to:", dest)

    if not file:
        os.makedirs(dest, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp_file:
            response = requests.get(url, stream=True)
            for chunk in response.iter_content(chunk_size=8192):
                tmp_file.write(chunk)
            tmp_file.flush()
            with tarfile.open(tmp_file.name, "r") as tar:
                tar.extractall(dest)
    else:
        dirname = os.path.dirname(dest)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        response = requests.get(url, stream=True)
        with open(dest, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

    print("downloading took:", time.time() - start)

class Predictor(BasePredictor):
    def setup(self) -> None:
        start = time.time()

        self.weights_cache = WeightsDownloadCache()
        self.last_loaded_loras = {}

        print("Loading safety checker...")
        if not os.path.exists(SAFETY_CACHE):
            download_weights(SAFETY_URL, SAFETY_CACHE)
        self.safety_checker = StableDiffusionSafetyChecker.from_pretrained(
            SAFETY_CACHE, torch_dtype=torch.float16
        ).to("cuda")
        self.feature_extractor = CLIPImageProcessor.from_pretrained(FEATURE_EXTRACTOR)

        print("Loading Stable Diffusion txt2img Pipeline")
        try:
            # Try loading with components explicitly to match architecture correctly
            print("Loading model with explicit architecture...")
            
            # Standard SD 1.5 architecture components
            vae = AutoencoderKL.from_pretrained(
                "stabilityai/sd-vae-ft-mse", 
                torch_dtype=torch.float16
            ).to("cuda")
            
            text_encoder = CLIPTextModel.from_pretrained(
                "runwayml/stable-diffusion-v1-5", 
                subfolder="text_encoder",
                torch_dtype=torch.float16
            ).to("cuda")
            
            tokenizer = CLIPTokenizer.from_pretrained(
                "runwayml/stable-diffusion-v1-5", 
                subfolder="tokenizer"
            )
            
            unet = UNet2DConditionModel.from_pretrained(
                "runwayml/stable-diffusion-v1-5", 
                subfolder="unet",
                torch_dtype=torch.float16
            ).to("cuda")
            
            scheduler = PNDMScheduler.from_pretrained(
                "runwayml/stable-diffusion-v1-5", 
                subfolder="scheduler"
            )
            
            # Create pipeline with explicit components
            self.txt2img_pipe = StableDiffusionPipeline(
                vae=vae,
                text_encoder=text_encoder,
                tokenizer=tokenizer,
                unet=unet,
                scheduler=scheduler,
                safety_checker=self.safety_checker,
                feature_extractor=self.feature_extractor,
                requires_safety_checker=False
            )
            
            # Now load the custom model weights over these base components
            print(f"Loading weights from {model_path}")
            self.txt2img_pipe.load_attn_procs(model_path)
            
        except Exception as e:
            print(f"Error loading with explicit architecture: {e}")
            print("Falling back to standard loading method...")
            
            # Fallback to standard loading method
            self.txt2img_pipe = StableDiffusionPipeline.from_single_file(
                model_path,
                torch_dtype=torch.float16,
                variant="fp16",
                use_safetensors=True
            ).to("cuda")

        patch_unet_get_aug_embed(self.txt2img_pipe.unet)
        self.txt2img_pipe.__class__.load_lora_into_transformer = classmethod(
            load_lora_into_transformer
        )

        print("Loading Stable Diffusion img2img pipeline")
        self.img2img_pipe = StableDiffusionImg2ImgPipeline(
            vae=self.txt2img_pipe.vae,
            text_encoder=self.txt2img_pipe.text_encoder,
            tokenizer=self.txt2img_pipe.tokenizer,
            unet=self.txt2img_pipe.unet,
            scheduler=self.txt2img_pipe.scheduler,
            safety_checker=self.txt2img_pipe.safety_checker,
            feature_extractor=self.txt2img_pipe.feature_extractor,
            requires_safety_checker=False
        ).to("cuda")

        patch_unet_get_aug_embed(self.img2img_pipe.unet)
        self.img2img_pipe.__class__.load_lora_into_transformer = classmethod(
            load_lora_into_transformer
        )
        
        print("setup took:", time.time() - start)

    @torch.amp.autocast('cuda')
    def run_safety_checker(self, image):
        safety_checker_input = self.feature_extractor(image, return_tensors="pt").to("cuda")
        np_image = [np.array(val) for val in image]
        image, has_nsfw_concept = self.safety_checker(
            images=np_image,
            clip_input=safety_checker_input.pixel_values.to(torch.float16),
        )
        return image, has_nsfw_concept

    def aspect_ratio_to_width_height(self, aspect_ratio: str) -> tuple[int, int]:
        return ASPECT_RATIOS[aspect_ratio]

    def get_image(self, image: str):
        image = Image.open(image).convert("RGB")
        transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Lambda(lambda x: 2.0 * x - 1.0),
        ])
        img: torch.Tensor = transform(image)
        return img[None, ...]

    @staticmethod
    def make_multiple_of_16(n):
        return ((n + 15) // 16) * 16

    def load_loras(self, hf_loras, lora_scales):
        # list of adapter names
        names = ['a','b','c','d','e','f','g','h','i','j','k','l','m','n','o','p','q','r','s','t','u','v','w','x','y','z']
        count = 0
        # loop through each lora
        for hf_lora in hf_loras:
            t1 = time.time()
            # Check for Huggingface Slug lucataco/flux-emoji
            if re.match(r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$", hf_lora):
                print(f"Downloading LoRA weights from - HF path: {hf_lora}")
                adapter_name = names[count]
                count += 1
                self.txt2img_pipe.load_lora_weights(hf_lora, adapter_name=adapter_name)
            # Check for Replicate tar file
            elif re.match(r"^https?://replicate.delivery/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+/trained_model.tar", hf_lora):
                print(f"Downloading LoRA weights from - Replicate URL: {hf_lora}")
                local_weights_cache = self.weights_cache.ensure(hf_lora)
                lora_path = os.path.join(local_weights_cache, "output/flux_train_replicate/lora.safetensors")
                adapter_name = names[count]
                count += 1
                self.txt2img_pipe.load_lora_weights(lora_path, adapter_name=adapter_name)
            # Check for Huggingface URL
            elif re.match(r"^https?://huggingface.co", hf_lora):
                print(f"Downloading LoRA weights from - HF URL: {hf_lora}")
                huggingface_slug = re.search(r"^https?://huggingface.co/([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)", hf_lora).group(1)
                weight_name = hf_lora.split('/')[-1]
                print(f"HuggingFace slug from URL: {huggingface_slug}, weight name: {weight_name}")
                adapter_name = names[count]
                count += 1
                self.txt2img_pipe.load_lora_weights(huggingface_slug, weight_name=weight_name)
            # Check for Civitai URL
            elif re.match(r"^https?://civitai.com/api/download/models/[0-9]+\?type=Model&format=SafeTensor", hf_lora):
                # split url to get first part of the url, everythin before '?type'
                civitai_slug = hf_lora.split('?type')[0]
                print(f"Downloading LoRA weights from - Civitai URL: {civitai_slug}")
                lora_path = self.weights_cache.ensure(hf_lora, file=True)
                adapter_name = names[count]
                count += 1
                self.txt2img_pipe.load_lora_weights(lora_path, adapter_name=adapter_name)
            # Check for URL to a .safetensors file
            elif hf_lora.endswith('.safetensors'):
                print(f"Downloading LoRA weights from - safetensor URL: {hf_lora}")
                try:
                    lora_path = self.weights_cache.ensure(hf_lora, file=True)
                except Exception as e:
                    print(f"Error downloading LoRA weights: {e}")
                    continue
                adapter_name = names[count]
                count += 1
                self.txt2img_pipe.load_lora_weights(lora_path, adapter_name=adapter_name)
            else:
                raise Exception(f"Invalid lora, must be either a: HuggingFace path, Replicate model.tar, or a URL to a .safetensors file: {hf_lora}")
            t2 = time.time()
            print(f"Loading LoRA took: {t2 - t1:.2f} seconds")
        adapter_names = names[:count]
        adapter_weights = lora_scales[:count]
        # print(f"adapter_names: {adapter_names}")
        # print(f"adapter_weights: {adapter_weights}")
        self.last_loaded_loras = hf_loras
        self.txt2img_pipe.set_adapters(adapter_names, adapter_weights=adapter_weights)
            
    @torch.inference_mode()
    def predict(
        self,
        prompt: str = Input(description="Prompt for generated image"),
        aspect_ratio: str = Input(
            description="Aspect ratio for the generated image",
            choices=list(ASPECT_RATIOS.keys()),
            default="1:1"
        ),
        image: Path = Input(
            description="Input image for image to image mode. The aspect ratio of your output will match this image",
            default=None,
        ),
        prompt_strength: float = Input(
            description="Prompt strength (or denoising strength) when using image to image. 1.0 corresponds to full destruction of information in image.",
            ge=0,le=1,default=0.8,
        ),
        num_outputs: int = Input(
            description="Number of images to output.",
            ge=1,
            le=4,
            default=1,
        ),
        num_inference_steps: int = Input(
            description="Number of inference steps",
            ge=1,le=50,default=28,
        ),
        guidance_scale: float = Input(
            description="Guidance scale for the diffusion process",
            ge=0,le=10,default=3.5,
        ),
        seed: int = Input(description="Random seed. Set for reproducible generation", default=None),
        output_format: str = Input(
            description="Format of the output images",
            choices=["webp", "jpg", "png"],
            default="webp",
        ),
        output_quality: int = Input(
            description="Quality when saving the output images, from 0 to 100. 100 is best quality, 0 is lowest quality. Not relevant for .png outputs",
            default=80,
            ge=0,
            le=100,
        ),
        hf_loras: list[str] = Input(
            description="Huggingface path, or URL to the LoRA weights. Ex: alvdansen/frosting_lane_flux",
            default=None,
        ),
        lora_scales: list[float] = Input(
            description="Scale for the LoRA weights. Default value is 0.8",
            default=None,
        ),
        disable_safety_checker: bool = Input(
            description="Disable safety checker for generated images. This feature is only available through the API. See [https://replicate.com/docs/how-does-replicate-work#safety](https://replicate.com/docs/how-does-replicate-work#safety)",
            default=True,
        ),
    ) -> List[Path]:
        """Run a single prediction on the model"""
        if seed is None:
            seed = int.from_bytes(os.urandom(2), "big")
        print(f"Using seed: {seed}")

        # Truncate prompt if too long
        original_prompt = prompt
        prompt = truncate_prompt(prompt)
        if prompt != original_prompt:
            print(f"Prompt was truncated from {len(original_prompt.split())} to {len(prompt.split())} tokens")

        width, height = self.aspect_ratio_to_width_height(aspect_ratio)
        max_sequence_length=512

        flux_kwargs = {"width": width, "height": height}
        print(f"Prompt: {prompt}")
        device = self.txt2img_pipe.device
        
        if image:
            pipe = self.img2img_pipe
            print("img2img mode")
            init_image = self.get_image(image)
            width = init_image.shape[-1]
            height = init_image.shape[-2]
            print(f"Input image size: {width}x{height}")
            # Calculate the scaling factor if the image exceeds MAX_IMAGE_SIZE
            scale = min(MAX_IMAGE_SIZE / width, MAX_IMAGE_SIZE / height, 1)
            if scale < 1:
                width = int(width * scale)
                height = int(height * scale)
                print(f"Scaling image down to {width}x{height}")

            # Round image width and height to nearest multiple of 16
            width = self.make_multiple_of_16(width)
            height = self.make_multiple_of_16(height)
            print(f"Input image size set to: {width}x{height}")
            # Resize
            init_image = init_image.to(device)
            init_image = torch.nn.functional.interpolate(init_image, (height, width))
            init_image = init_image.to(torch.bfloat16)
            # Set params
            flux_kwargs["image"] = init_image
            flux_kwargs["strength"] = prompt_strength
        else:
            print("txt2img mode")
            pipe = self.txt2img_pipe
        
        if hf_loras:
            flux_kwargs["joint_attention_kwargs"] = {"scale": 1.0}
            # check if loras are new
            if hf_loras != self.last_loaded_loras:
                pipe.unload_lora_weights()
                # Check for hf_loras and lora_scales
                if hf_loras and not lora_scales:
                    # If no lora_scales are provided, use 0.8 for each lora
                    lora_scales = [0.8] * len(hf_loras)
                    self.load_loras(hf_loras, lora_scales)
                elif hf_loras and len(lora_scales) == 1:
                    # If only one lora_scale is provided, use it for all loras
                    lora_scales = [lora_scales[0]] * len(hf_loras)
                    self.load_loras(hf_loras, lora_scales)
                elif hf_loras and len(lora_scales) >= len(hf_loras):
                    # If lora_scales are provided, use them for each lora
                    self.load_loras(hf_loras, lora_scales)
        else:
            flux_kwargs["joint_attention_kwargs"] = None
            pipe.unload_lora_weights()

        # Ensure the pipeline is on GPU
        pipe = pipe.to("cuda")

        generator = torch.Generator("cuda").manual_seed(seed)

        common_args = {
            "prompt": [prompt] * num_outputs,
            "guidance_scale": guidance_scale,
            "generator": generator,
            "num_inference_steps": num_inference_steps,
            "output_type": "pil"
        }
        
        # Try with error handling
        try:
            output = pipe(
                **common_args,
                **flux_kwargs
            )
        except RuntimeError as e:
            print(f"Error during inference: {e}")
            # If there's a shape mismatch error, try with padding attention
            if "mat1 and mat2 shapes cannot be multiplied" in str(e):
                print("Attempting with attention processor patch...")
                # Apply a patch for cross-attention processors
                from diffusers.models.attention_processor import AttnProcessor2_0
                
                # Switch to standard attention processor
                pipe.unet.set_attn_processor(AttnProcessor2_0())
                
                # Try again with modified pipeline
                output = pipe(
                    **common_args,
                    **flux_kwargs
                )
            else:
                raise e

        if not disable_safety_checker:
            _, has_nsfw_content = self.run_safety_checker(output.images)

        output_paths = []
        for i, image in enumerate(output.images):
            if not disable_safety_checker and has_nsfw_content[i]:
                print(f"NSFW content detected in image {i}")
                continue
            output_path = f"/tmp/out-{i}.{output_format}"
            if output_format != 'png':
                image.save(output_path, quality=output_quality, optimize=True)
            else:
                image.save(output_path)
            output_paths.append(Path(output_path))

        if len(output_paths) == 0:
            raise Exception("NSFW content detected. Try running it again, or try a different prompt.")

        return output_paths
