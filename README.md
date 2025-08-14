# Factory_Floor_Digital_Twin
## Prerequisites

Before you begin, ensure you have the following installed on your system:

*   **Python 3.7+**: Download and install Python from the [official website](https://www.python.org/downloads/).
*   **pip**: The Python package installer. It's usually included with Python installations.

## Installation

1.  **Install project dependencies**: Use `pip` to install the required libraries. This includes FastAPI, Uvicorn, and Jinja2.

    ```bash
    pip install "fastapi[all]"
    ```

    - **paho-mqtt**: Used for MQTT messaging between devices and the backend, enabling real-time communication in the digital twin environment.
    - **python-multipart**: Required by FastAPI to handle file uploads via multipart/form-data requests.


## Running the Application

To start the application, navigate to your project's root directory in the terminal and run the following command. The `--reload` flag enables hot-reloading, so the server will automatically restart when you make changes to your code.


uvicorn backend:app --host 0.0.0.0 --port 8000

## Docker Support

This project is fully containerized with Docker, allowing users to run the application without manually installing Python or dependencies on their local machine.
We have included the following Docker-related files in the repository:
Dockerfile – Defines the application’s Docker image, including the base image, dependencies, and build instructions.
docker-compose.yml – Provides a simple way to start the application (and any additional services) with a single command.
.dockerignore – Lists files and directories that should be excluded from the Docker build context to reduce image size.
requirements.txt – Specifies all Python dependencies required by the application.
So, anyone can clone the repository, build the Docker image, and run the application in a fully isolated environment—without having to manually install Python or any dependencies.

## Run with Docker Compose (Recommended)

1. Build and start the container
docker-compose up --build
2. Access the application in your browser
#    If running locally:
http://localhost:8000
3. Stop the container
docker-compose down

## Run with Docker Only
1. Build the Docker image
docker build -t factory-floor-digital-twin .
2. Run the container
docker run -d -p 8000:8000 factory-floor-digital-twin
3. Access the application
http://localhost:8000

## Documentation diagrams:
![Architecture Diagram](Documentation/architecture_diagram.png)
![Process Flow Diagram](Documentation/process_flow_diagram.png)

