import pickle
from api.feature_extractors import UrgencyFeatureExtractor, URLFeatureExtractor

MODEL_PATH = 'models/phishing_model.pkl'

def load_model():
    """
    Load the machine learning model from disk.
    
    Returns:
        model: The deserialized machine learning model.
    """
    with open(MODEL_PATH, 'rb') as f:
        model = pickle.load(f)
    return model
