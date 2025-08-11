# Factory_Floor_Digital_Twin
## Prerequisites

Before you begin, ensure you have the following installed on your system:

*   **Python 3.7+**: Download and install Python from the [official website](https://www.python.org/downloads/).
*   **pip**: The Python package installer. It's usually included with Python installations.

## Installation

1.  **Install project dependencies**: Use `pip` to install the required libraries. This includes FastAPI, Uvicorn, and Jinja2.

    ```bash
    pip install fastapi uvicorn jinja2
    ```

2.  **Install Uvicorn with standard extras**: This command installs Uvicorn with additional features needed for development, such as `watchfiles` for hot-reloading.

    ```bash
    pip install 'uvicorn[standard]'
    ```

## Running the Application

To start the application, navigate to your project's root directory in the terminal and run the following command. The `--reload` flag enables hot-reloading, so the server will automatically restart when you make changes to your code.

```bash
uvicorn backend:app --reload --host 0.0.0.0 --port 8000
