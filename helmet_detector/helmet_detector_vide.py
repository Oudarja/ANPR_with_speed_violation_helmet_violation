import cv2
import cvzone
from ultralytics import YOLO
import time
# Initialize video capture
# PLease In time of running project save video file
# video_path = "/media/tanmoy002/HDD/Bike_helmet_detector/traffic/NVR_ch1_main_20260330000000_20260330235960.dav"
# cap = cv2.VideoCapture(video_path)

# Get video properties for output
# frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
# frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
# fps = cap.get(cv2.CAP_PROP_FPS)

# Load YOLO model
model = YOLO("helmet.pt")

# Define class names
classNames = ['With Helmet', 'Without Helmet']

# Set minimum confidence threshold
# here on frame_resized deteion of helemet have to be done

def detect_helmet(frame_resized):
    """
    Run helmet detection on any BGR image or crop.
    
    Mirrors exactly how helmet_detector.py calls the model:
        results = yolo_model(img)
    
    Args:
        img : BGR numpy array
              Can be a full frame OR a motorbike crop from camera_worker.
              Model finds helmet/no-helmet bounding boxes inside it.
    
    Returns:
        YOLO results list — same format as model(img)
        Each result has .boxes with:
            .xyxy[0]  → (x1, y1, x2, y2) relative to img
            .conf[0]  → confidence score
            .cls[0]   → 0 = 'With Helmet', 1 = 'Without Helmet'
    """
    return model(frame_resized, verbose=False)
        