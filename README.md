# Orders2 TUI

The numeric menu calls the six API functions in `1.cpp` and displays their raw responses. Changing leverage, creating orders, and stopping orders require typing `YES`. These operations use the live Gate account. Starting or exiting the application sends no requests.

## Configuration

Create a local `.env` using the keys documented at the top of `1.cpp`. The application reads `.env` from the current working directory, or accepts a file path as its only argument. Configuration is reloaded before each operation. Never commit credentials or include them in build artifacts.

## GitHub Actions

This directory must be the GitHub repository root. Pushes, pull requests, and manual runs build both targets. Download `orders2-windows-x64` from the workflow's artifacts and extract `orders2.exe`. Dependencies and the MSVC runtime are linked statically.

```powershell
.\orders2.exe C:\private\orders2.env
```

Linux images target `linux/amd64`. Successful runs on the default branch publish `latest` and `sha-<commit>` to `ghcr.io/<owner>/<repository>` (lowercase). Pull requests build and test without publishing. The workflow uses the built-in `GITHUB_TOKEN` with package write permission.

```sh
docker pull ghcr.io/<owner>/<repository>:latest
docker run --rm -it \
  --mount type=bind,src=/absolute/path/to/orders2.env,dst=/app/.env,readonly \
  ghcr.io/<owner>/<repository>:latest
```

Use `-it` for the interactive menu. The image contains no credentials. For a private GHCR package, authenticate with `docker login ghcr.io` before pulling; anonymous pulls require public package visibility.

Both CI jobs test menu startup and exit without credentials or network API calls before uploading or publishing.
