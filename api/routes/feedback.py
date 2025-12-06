import os
import csv
import logging
from flask import Blueprint, request, jsonify

feedback_bp = Blueprint("feedback", __name__)

# Define the path where feedback will be stored
FEEDBACK_CSV = os.path.join("data", "feedback", "misclassified_emails.csv")
os.makedirs(os.path.dirname(FEEDBACK_CSV), exist_ok=True)

def save_feedback(email_text, predicted_label, correct_label):
    """
    Append the misclassified email data to a CSV file.
    This record can later be used for retraining the model.
    """
    with open(FEEDBACK_CSV, 'a', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([email_text, predicted_label, correct_label])

@feedback_bp.route('/feedback', methods=['POST'])
def feedback_route():
    try:
        # Parse JSON data
        data = request.get_json()
        if data is None:
            return jsonify({"error": "No JSON data provided"}), 400

        email_text = data.get("email_text", "")
        predicted_label = data.get("predicted_label", "")
        correct_label = data.get("correct_label", "")

        # Validate the required field
        if not email_text:
            return jsonify({"error": "Email text is required"}), 400

        # Save the feedback record to CSV
        save_feedback(email_text, predicted_label, correct_label)
        return jsonify({"status": "Feedback saved successfully"}), 200

    except Exception as e:
        logging.error("Error in feedback_route: %s", e)
        return jsonify({"error": "An error occurred while saving feedback"}), 500
