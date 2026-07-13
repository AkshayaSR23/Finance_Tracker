# 1. Start from an official Python image
FROM python:3.12-slim

# 2. Create a working directory
WORKDIR /app

# 3. Copy requirements.txt first
COPY requirements.txt .

# 4. Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy the rest of the project
COPY . .

# 6. Streamlit uses port 8501
EXPOSE 8501

# 7. Run the application
CMD ["streamlit", "run", "finance_tracker.py", "--server.address=0.0.0.0"]