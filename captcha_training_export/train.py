"""Auto-generated YOLOv8 training script.
Samples: 1  ·  Classes: 1
"""
from ultralytics import YOLO

# Pre-trained YOLOv8 nano (más rápido para fine-tune)
model = YOLO("yolov8n.pt")

# Train on our CAPTCHA dataset
model.train(
    data="data.yaml",
    epochs=50,
    imgsz=640,
    batch=16,
    device=0,  # GPU 0, o "cpu" si no hay GPU
    name="captcha_model",
)

# Validate
metrics = model.val()
print(f"mAP50: {metrics.box.map50}")

# Export a ONNX (listo para captcha_cv_solver)
model.export(format="onnx", opset=13, dynamic=True, simplify=True)
print("Modelo ONNX exportado en runs/detect/captcha_model/weights/best.onnx")
print("Importa con: POST /api/captcha-cv/import-model")
