"""Vision transfer-learning fine-tune flow (010 US1, T183/T184 — FR-088/FR-089/FR-098).

The **lowest-risk** new modality: torchvision is already a frozen dep, and 009's BentoML vision service
already loads a `model.pt` carrying `{state_dict, categories}` from the MinIO `models` bucket — so this
flow fine-tunes *into that exact shape*. Default approach is conservative transfer learning:

  load a torchvision backbone (ImageNet-pretrained) → **freeze the backbone** → **swap the classifier
  head** to the dataset's class count → train the head for a few epochs at a small LR → (optional) a
  short low-LR unfreeze pass → write `model.pt` {state_dict, categories} → upload → register a version
  tagged `task=image-classification` / `serving_engine=bentoml` / `framework=torchvision` / lineage.

These hyperparameters (epochs/LR/batch/unfreeze) are **defaults exposed on the Runs form** (configurable,
HPO-tunable by 012) — 010 does not hard-pin them. Like every fine-tune this is a GPU lease tenant; the
trainer daemon (`trainer.py`) holds the lease, so this flow just trains and frees the GPU promptly.

**Default backbone is `mobilenet_v2`** to match the arch the 009 vision service constructs; a promoted
version is then servable with no server change beyond sizing the head from `categories` (which the
service does as of 010). Dataset shape: JSONL rows `{"image_b64": <png/jpeg bytes b64>, "label": <str>}`.
"""
import base64
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # _common/lineage import as module or script
from _common import MLFLOW_URI, MODELS_BUCKET, _log, fetch_jsonl, flow, free_cuda, s3_client  # noqa: E402
from lineage import lineage_tags, link_parent_run, resolve_parent  # noqa: E402

TASK = "image-classification"
DEFAULT_BACKBONE = os.getenv("VISION_BASE_ARCH", "mobilenet_v2")  # matches the 009 service arch


def _build_model(backbone: str, num_classes: int, pretrained: bool):
    """A torchvision classifier with its head swapped to `num_classes`. Default `mobilenet_v2`; the
    backbone is loaded ImageNet-pretrained for transfer learning unless a chained warm-start overrides
    the weights afterward."""
    import torch.nn as nn
    import torchvision.models as M

    if backbone != "mobilenet_v2":
        # 010 default + the 009 service both assume mobilenet_v2; other backbones would need the
        # service to construct a matching arch (out of 010 scope), so refuse early with a clear error.
        raise ValueError(f"unsupported backbone {backbone!r}: 010 serves mobilenet_v2 (the 009 vision "
                         f"service arch); add an arch map to the service to serve others")
    weights = M.MobileNet_V2_Weights.IMAGENET1K_V2 if pretrained else None
    model = M.mobilenet_v2(weights=weights)
    in_features = model.classifier[1].in_features
    model.classifier[1] = nn.Linear(in_features, num_classes)  # swap head to the dataset's classes
    return model


def _preprocess():
    from torchvision import transforms
    return transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])


def _parse_rows(rows: list):
    """Validate the vision dataset shape and return (images[PIL], labels[str], categories[sorted]).
    Errors clearly if the pinned version isn't labeled images (Edge case: dataset shape mismatch)."""
    from PIL import Image

    images, labels = [], []
    for i, r in enumerate(rows):
        b64, label = r.get("image_b64"), r.get("label")
        if not b64 or label is None:
            raise ValueError(f"vision dataset row {i} missing image_b64/label — expected labeled images")
        images.append(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"))
        labels.append(str(label))
    if not images:
        raise ValueError("vision dataset has no usable {image_b64,label} rows")
    categories = sorted(set(labels))
    return images, labels, categories


def _warm_start(model, parent_source: str):
    """Chaining (US4): load the parent `model.pt` backbone weights into the current model as a trainable
    start (strict=False, so a head sized to a different class count is simply re-initialized while the
    backbone transfers). Mirrors the adapter-chaining intent for a full-weight modality (FR-096)."""
    import torch

    bucket, key = parent_source.replace("s3://", "", 1).split("/", 1)
    blob = s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    ckpt = torch.load(io.BytesIO(blob), map_location="cpu", weights_only=False)
    missing, unexpected = model.load_state_dict(ckpt["state_dict"], strict=False)
    _log(f"warm-started from parent ({len(missing)} fresh / {len(unexpected)} dropped params — "
         f"head re-init on class-count change is expected)")


def _train(images, labels, categories, *, backbone, epochs, lr, batch_size, unfreeze_epochs,
           seed, parent_source):
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset

    torch.manual_seed(seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    _log(f"vision fine-tune on {device}: {len(images)} imgs, {len(categories)} classes, backbone={backbone}")

    cat_index = {c: i for i, c in enumerate(categories)}
    pre = _preprocess()
    x = torch.stack([pre(im) for im in images])
    y = torch.tensor([cat_index[l] for l in labels], dtype=torch.long)

    model = _build_model(backbone, len(categories), pretrained=parent_source is None)
    if parent_source:
        _warm_start(model, parent_source)
    for p in model.features.parameters():  # freeze the backbone (transfer learning default)
        p.requires_grad = False
    model = model.to(device)

    loader = DataLoader(TensorDataset(x, y), batch_size=batch_size, shuffle=True)
    loss_fn = nn.CrossEntropyLoss()

    def _run_epochs(n, lr_, train_backbone):
        for p in model.features.parameters():
            p.requires_grad = train_backbone
        params = [p for p in model.parameters() if p.requires_grad]
        opt = torch.optim.Adam(params, lr=lr_)
        last = 0.0
        for ep in range(n):
            model.train()
            tot, count = 0.0, 0
            for xb, yb in loader:
                xb, yb = xb.to(device), yb.to(device)
                opt.zero_grad()
                loss = loss_fn(model(xb), yb)
                loss.backward()
                opt.step()
                tot += float(loss) * len(xb)
                count += len(xb)
            last = tot / max(count, 1)
            _log(f"  epoch {ep + 1}/{n} (backbone={'on' if train_backbone else 'frozen'}): loss={last:.4f}")
        return last

    train_loss = _run_epochs(epochs, lr, train_backbone=False)        # head-only pass
    if unfreeze_epochs > 0:
        train_loss = _run_epochs(unfreeze_epochs, lr / 10.0, train_backbone=True)  # low-LR fine pass

    # Train accuracy as a cheap, recorded eval signal (a held-out split would be better but the demo
    # datasets are tiny; 012/013 own proper eval). Inference under no_grad on the same device.
    model.eval()
    with torch.no_grad():
        preds = model(x.to(device)).argmax(1).cpu()
    train_acc = float((preds == y).float().mean())

    state_dict = {k: v.cpu() for k, v in model.state_dict().items()}
    metrics = {"train_loss": train_loss, "train_accuracy": train_acc,
               "num_classes": len(categories), "epochs": epochs, "device": device}
    del model
    free_cuda()  # nothing stays resident after training (Principle II)
    _log(f"vision training done: loss={train_loss:.4f} acc={train_acc:.4f}")
    return state_dict, metrics, device


def _register(output_name, state_dict, categories, run_id, *, backbone, dataset_name,
              dataset_version, base_model, parent_version, parent_run_id, device):
    """Write `model.pt` {state_dict, categories} (the 009 BentoML load shape), upload to MinIO, and
    register an MLflow version tagged for image-classification serving + lineage (FR-089)."""
    import torch
    from mlflow.exceptions import MlflowException
    from mlflow.tracking import MlflowClient

    buf = io.BytesIO()
    torch.save({"state_dict": state_dict, "categories": list(categories)}, buf)
    key = f"{output_name}/{run_id}/model.pt"
    s3_client().put_object(Bucket=MODELS_BUCKET, Key=key, Body=buf.getvalue())
    source = f"s3://{MODELS_BUCKET}/{key}"

    c = MlflowClient(tracking_uri=MLFLOW_URI)
    try:
        c.create_registered_model(output_name)
    except MlflowException:
        pass
    tags = {"kind": "vision-classifier", "arch": backbone, "framework": "torchvision",
            "task": TASK, "serving_engine": "bentoml", "device": device,
            **lineage_tags(base_model, dataset_name, dataset_version, parent_version, parent_run_id)}
    mv = c.create_model_version(name=output_name, source=source, run_id=run_id, tags=tags)
    _log(f"registered {output_name} v{mv.version} <- run {run_id} ({len(categories)} classes, "
         f"task={TASK}, NOT auto-promoted — promotion stays manual)")
    return {"name": output_name, "version": str(mv.version), "source": source}


@flow(name="vision-finetune")
def vision_finetune_flow(dataset_name: str, dataset_version: str, output_name: str,
                         base_model: str | None = DEFAULT_BACKBONE, epochs: int = 3, lr: float = 1e-3,
                         batch_size: int = 8, unfreeze_epochs: int = 0, seed: int = 0,
                         parent_version: str | None = None) -> dict:
    """End-to-end vision transfer-learning fine-tune → registered, servable image-classification version.

    `parent_version` (optional) chains from a prior registered version of `output_name` (US4): its
    backbone weights warm-start the new model; a parent whose `task` isn't image-classification is
    rejected before training (`resolve_parent`).
    """
    import mlflow

    backbone = base_model or DEFAULT_BACKBONE
    parent = resolve_parent(output_name, parent_version, TASK) if parent_version else None

    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment("vision-finetune")
    with mlflow.start_run() as run:
        run_id = run.info.run_id
        params = dict(modality="vision", backbone=backbone, dataset_name=dataset_name,
                      dataset_version=dataset_version, output_name=output_name, epochs=epochs,
                      lr=lr, batch_size=batch_size, unfreeze_epochs=unfreeze_epochs, seed=seed,
                      parent_version=parent_version)
        mlflow.log_params(params)  # full config recorded → reproducible (FR-098/SC-062)
        link_parent_run(parent["run_id"] if parent else None)

        rows = fetch_jsonl(dataset_name, dataset_version)
        images, labels, categories = _parse_rows(rows)
        try:
            state_dict, metrics, device = _train(
                images, labels, categories, backbone=backbone, epochs=epochs, lr=lr,
                batch_size=batch_size, unfreeze_epochs=unfreeze_epochs, seed=seed,
                parent_source=parent["source"] if parent else None)
            mlflow.log_metrics({k: v for k, v in metrics.items() if isinstance(v, (int, float))})
            mlflow.set_tag("device", device)
            mv = _register(output_name, state_dict, categories, run_id, backbone=backbone,
                           dataset_name=dataset_name, dataset_version=dataset_version,
                           base_model=backbone, parent_version=parent_version,
                           parent_run_id=parent["run_id"] if parent else None, device=device)
        finally:
            free_cuda()  # release VRAM whether training/registration succeeded or raised (Principle II)
        mlflow.set_tag("registered_version", mv["version"])
        return {"run_id": run_id, "model": mv, "metrics": metrics, "params": params}


if __name__ == "__main__":
    import json
    a = sys.argv
    print(json.dumps(vision_finetune_flow(
        dataset_name=a[1], dataset_version=a[2], output_name=a[3],
        epochs=int(a[4]) if len(a) > 4 else 3), indent=2))
