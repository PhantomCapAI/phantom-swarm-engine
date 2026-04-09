"""Segmind AI art generation for swarm token launches."""

import os
import base64
import hashlib
import time
import httpx

SEGMIND_API_KEY = os.environ.get("SEGMIND_API_KEY", "")
BASE_URL = "https://api.segmind.com/v1"

# Model routing: style keyword → (endpoint, cost per image)
MODELS = {
    "photorealistic": ("flux-pro", 0.069),
    "typography": ("seedream-4", 0.035),
    "text-heavy": ("seedream-4", 0.035),
    "abstract": ("flux-pro", 0.069),
    "geometric": ("flux-pro", 0.069),
    "fast": ("p-image", 0.005),
    "draft": ("p-image", 0.005),
    "iteration": ("p-image", 0.005),
    "default": ("p-image", 0.005),
}


def pick_model(style: str) -> tuple[str, float]:
    """Pick model based on style keywords."""
    style_lower = style.lower()
    for keyword, (model, cost) in MODELS.items():
        if keyword in style_lower:
            return model, cost
    return "p-image", 0.005


async def generate_art(prompt: str, style: str = "default", aspect: str = "1:1") -> dict:
    """Generate an image via Segmind API.

    Returns: { image_url, model_used, cost, error }
    """
    if not SEGMIND_API_KEY:
        return {"image_url": None, "model_used": None, "cost": 0, "error": "SEGMIND_API_KEY not set"}

    model, cost = pick_model(style)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{BASE_URL}/{model}",
                headers={
                    "x-api-key": SEGMIND_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "prompt": prompt,
                    "steps": 25 if model == "flux-pro" else 4,
                    "aspect_ratio": aspect,
                    "seed": int(time.time()) % 100000,
                    "output_format": "png",
                    "output_quality": 90,
                    "base64": True,
                },
            )

            if resp.status_code != 200:
                return {"image_url": None, "model_used": model, "cost": 0, "error": f"Segmind {resp.status_code}: {resp.text[:200]}"}

            # Response is base64 image data or raw bytes
            content_type = resp.headers.get("content-type", "")

            if "json" in content_type:
                data = resp.json()
                img_b64 = data.get("image") or data.get("output") or ""
            else:
                # Raw image bytes — encode to base64
                img_b64 = base64.b64encode(resp.content).decode()

            if not img_b64:
                return {"image_url": None, "model_used": model, "cost": 0, "error": "No image data in response"}

            # Generate a stable filename from prompt hash
            prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:12]
            image_url = f"data:image/png;base64,{img_b64[:100]}..."  # Truncated for URL

            return {
                "image_b64": img_b64,
                "image_url": f"/swarm/art/image/{prompt_hash}",
                "model_used": model,
                "cost": cost,
                "error": None,
            }

    except Exception as e:
        return {"image_url": None, "model_used": model, "cost": 0, "error": str(e)[:200]}


# In-memory image store (keyed by hash)
_image_store: dict[str, bytes] = {}


def store_image(prompt_hash: str, image_b64: str) -> str:
    """Store base64 image and return retrieval URL."""
    _image_store[prompt_hash] = base64.b64decode(image_b64)
    return f"/swarm/art/image/{prompt_hash}"


def get_image(prompt_hash: str) -> bytes | None:
    """Retrieve stored image bytes."""
    return _image_store.get(prompt_hash)
