# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Install dependencies first so they are cached between builds
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy the current directory contents into the container at /app
COPY . /app

# Make port 8080 available to the world outside this container
EXPOSE 8080

# Chat memory lives in memory, so keep a single worker and use threads for concurrency
CMD gunicorn --bind :8080 --workers 1 --threads 8 --timeout 120 app:app
