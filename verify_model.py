# verify_model.py
import os
import pickle

model_path = 'models/phishing_model.pkl'

# Check if the file exists and print its modification time
if os.path.exists(model_path):
    print("Model file found:", model_path)
    print("Last modified:", os.path.getmtime(model_path))
else:
    print("Model file not found!")

# Load the model from the pickle file
with open(model_path, 'rb') as f:
    model_pipeline = pickle.load(f)

# Inspect the pipeline steps
print("\nPipeline steps:")
for name, step in model_pipeline.named_steps.items():
    print(f" - {name}: {step}")

# Test the model with a dummy input
test_email = "This is a sample email for test purposes."
risk_score = model_pipeline.predict_proba([test_email])[0][1]
print(f"\nTest input risk score: {risk_score:.2f}")
