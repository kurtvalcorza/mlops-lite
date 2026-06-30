"""In-process vision scorer (015) — top-1 labels from the in-memory torch model.

The trained torchvision classifier **is** the served artifact (009 BentoML loads the same
`{state_dict, categories}`), so there is no quantization gap: scoring runs the still-resident trained
model over the held-out benchmark images in-memory (D6). `make_predict_fn` returns a `predict_fn` closure
matching the eval harness seam `predict_fn(rows, modality, version) -> [label, ...]`, so the flow can
`score_and_log(..., make_predict_fn(model, categories, device))` while it still holds the model + lease.
"""
import base64
import io


def make_predict_fn(model, categories, device):
    """Build a `predict_fn(rows, modality, version)` that classifies each benchmark image with the
    in-memory `model` (already on `device`, in eval mode), mapping the argmax index to its category.

    `categories` is the same sorted class list registered in the model's `model.pt`, so a predicted
    index maps to the exact label the gate's `accuracy` metric compares against the row's `label`.
    """
    import torch
    from PIL import Image
    from torchvision import transforms

    pre = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    def predict_fn(rows, _modality, _version):
        imgs = [Image.open(io.BytesIO(base64.b64decode(r["image_b64"]))).convert("RGB") for r in rows]
        x = torch.stack([pre(im) for im in imgs]).to(device)
        model.eval()
        with torch.no_grad():
            idx = model(x).argmax(1).cpu().tolist()
        return [categories[i] if 0 <= i < len(categories) else "" for i in idx]

    return predict_fn
