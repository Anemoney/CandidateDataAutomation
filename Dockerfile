# Use an official lightweight Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies required by Playwright and Chromium
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    libglib2.0-0 \
    libnss3 \
    libxcb1 \
    libx11-xcb1 \
    libdbus-1-3 \
    libxss1 \
    libasound2 \
    fonts-liberation \
    libappindicator3-1 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium) globally
RUN playwright install chromium --with-deps

# Copy the application code
COPY . .

# Expose port 8000
EXPOSE 8000

# Run the Streamlit app, listening on Cloud Run's dynamic port
CMD ["sh", "-c", "streamlit run app.py --server.port=${PORT:-8080} --server.address=0.0.0.0"]
