# data_processing.py
import os
import pandas as pd  # type: ignore[reportMissingImports]
import re
import string

# Define the input and output file paths
raw_data_path = os.path.join("data", "raw", "emails.csv")
processed_data_path = os.path.join("data", "processed", "cleaned_emails.csv")

def preprocess_text(text):
    """Convert text to lowercase and remove punctuation."""
    # Convert to lowercase
    text = text.lower()
    # Remove punctuation using regex
    text = re.sub('[{}]'.format(re.escape(string.punctuation)), "", text)
    return text

def main():
    # Ensure the processed data folder exists
    os.makedirs(os.path.dirname(processed_data_path), exist_ok=True)

    try:
        # Read the CSV file containing raw email data
        df = pd.read_csv(raw_data_path)
    except Exception as e:
        print(f"Error reading {raw_data_path}: {e}")
        return

    # Check if the 'email' column exists
    if "email" not in df.columns:
        print("CSV file does not contain an 'email' column.")
        return

    # Create a new column 'cleaned_email' by applying the preprocessing function
    df["cleaned_email"] = df["email"].apply(preprocess_text)

    # Save the processed DataFrame to the specified output path
    df.to_csv(processed_data_path, index=False)
    print(f"Processed file saved to {processed_data_path}")

if __name__ == "__main__":
    main()
