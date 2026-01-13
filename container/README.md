# VEP Police Agent - Container Guide

This guide explains how to build, push, and run the VEP Police Agent as a containerized application using Podman.

## Overview

The VEP Police Agent is containerized for easy deployment. The container includes:
- Python 3.11 runtime
- Node.js (for MCP servers)
- All Python dependencies
- The complete application code

## Prerequisites

1. **Podman** - Container runtime (install from your distribution's package manager)
2. **Quay.io Account** - For pushing images (or use another registry)
3. **Credentials**:
   - Google API key (for Gemini LLM)
   - Google service account JSON (for Google Sheets)
   - GitHub token (optional, for GitHub MCP integration)

## Quick Start

### Build and Push to Quay.io

```bash
# From the project root
./container/build-and-push.sh
```

The script will:
- Prompt for quay.io credentials if not already logged in
- Build the container image
- Tag it with your username and push to quay.io

### Run the Container

```bash
podman run --rm \
  quay.io/YOUR_USERNAME/vep-police-agent:latest \
  --api-key "your-gemini-api-key" \
  --google-token "$(cat /path/to/google-credentials.json)"
```

## Detailed Usage

### Building the Container

#### Basic Build

```bash
./container/build-and-push.sh
```

#### Custom Configuration

You can customize the build using environment variables:

```bash
# Custom username
QUAY_USERNAME=myuser ./container/build-and-push.sh

# Custom image name
IMAGE_NAME=my-vep-agent ./container/build-and-push.sh

# Custom tag
IMAGE_TAG=v1.0.0 ./container/build-and-push.sh

# Also tag as "latest" (creates both tags)
ADD_LATEST_TAG=true IMAGE_TAG=v1.0.0 ./container/build-and-push.sh

# All together
QUAY_USERNAME=myuser IMAGE_NAME=my-vep-agent IMAGE_TAG=v1.0.0 ADD_LATEST_TAG=true ./container/build-and-push.sh
```

#### Manual Build (without pushing)

If you just want to build locally without pushing:

```bash
cd /path/to/vep-police-agent
podman build -f container/Containerfile -t vep-police-agent:local .
```

### Running the Container

#### Required Arguments

The container requires credentials to be passed via CLI arguments:

- `--api-key`: Your Gemini API key
- `--google-token`: Google service account JSON (can be a file path or JSON string)

#### Basic Run

```bash
podman run --rm \
  quay.io/YOUR_USERNAME/vep-police-agent:latest \
  --api-key "your-api-key" \
  --google-token "$(cat /path/to/google-credentials.json)"
```

#### Using File Paths

If your Google token is in a file:

```bash
podman run --rm \
  -v /path/to/credentials:/creds:ro \
  quay.io/YOUR_USERNAME/vep-police-agent:latest \
  --api-key "your-api-key" \
  --google-token "/creds/google-credentials.json"
```

#### With GitHub Token

```bash
podman run --rm \
  quay.io/YOUR_USERNAME/vep-police-agent:latest \
  --api-key "your-api-key" \
  --google-token "$(cat /path/to/google-credentials.json)" \
  --github-token "your-github-token"
```

#### Using Environment Variables

You can also pass credentials via environment variables:

```bash
podman run --rm \
  -e API_KEY="your-api-key" \
  -e GOOGLE_TOKEN="$(cat /path/to/google-credentials.json)" \
  -e GITHUB_TOKEN="your-github-token" \
  quay.io/YOUR_USERNAME/vep-police-agent:latest
```

Note: CLI arguments take precedence over environment variables.

### Image Tagging

The build script automatically uses:
- **Git commit hash** as the tag (if in a git repository)
- **"latest"** as fallback

You can override this with the `IMAGE_TAG` environment variable:

```bash
IMAGE_TAG=dev ./container/build-and-push.sh
IMAGE_TAG=v1.2.3 ./container/build-and-push.sh
```

#### Tagging with Both Custom Tag and "latest"

To tag the image with both your specified tag AND "latest" (useful for versioned releases that should also be available as "latest"):

```bash
ADD_LATEST_TAG=true IMAGE_TAG=v1.2.3 ./container/build-and-push.sh
```

This will create and push both:
- `quay.io/YOUR_USERNAME/vep-police-agent:v1.2.3`
- `quay.io/YOUR_USERNAME/vep-police-agent:latest`

Note: If `IMAGE_TAG` is already "latest", the `ADD_LATEST_TAG` flag has no effect.

## Configuration

### Build Script Options

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `QUAY_USERNAME` | `$(whoami)` | Your quay.io username |
| `IMAGE_NAME` | `vep-police-agent` | Name of the container image |
| `IMAGE_TAG` | Git commit hash or `latest` | Tag for the image |
| `ADD_LATEST_TAG` | `false` | If `true` or `1`, also tag the image as "latest" (in addition to IMAGE_TAG) |

### Container Runtime Options

| CLI Argument | Environment Variable | Description |
|-------------|---------------------|-------------|
| `--api-key` | `API_KEY` | Gemini API key for LLM |
| `--google-token` | `GOOGLE_TOKEN` | Google service account JSON |
| `--github-token` | `GITHUB_TOKEN` | GitHub token (optional) |

## Authentication

### Quay.io Login

The build script handles authentication automatically:

1. **First time**: You'll be prompted for username and password/token
2. **Subsequent builds**: Uses cached credentials (no prompt)

#### Manual Login

If you prefer to login manually:

```bash
podman login quay.io
```

#### Using Access Tokens

For CI/CD or automation, use access tokens:

```bash
echo "YOUR_QUAY_TOKEN" | podman login quay.io --username YOUR_USERNAME --password-stdin
```

## File Structure

```
vep-police-agent/
├── container/
│   ├── Containerfile          # Container definition
│   ├── build-and-push.sh      # Build and push script
│   └── README.md              # This file
├── requirements.txt           # Python dependencies
├── .containerignore           # Files to exclude from build
└── ... (application code)
```

## Troubleshooting

### Build Fails

**Issue**: Build fails with "file not found"
- **Solution**: Make sure you're running the script from the project root, or the script will change to the correct directory automatically

**Issue**: "podman: command not found"
- **Solution**: Install podman from your distribution's package manager

### Push Fails

**Issue**: "unauthorized: authentication required"
- **Solution**: Run `podman login quay.io` or let the build script prompt you

**Issue**: "repository does not exist"
- **Solution**: Create the repository on quay.io first, or ensure your username is correct

### Runtime Issues

**Issue**: "API_KEY not found"
- **Solution**: Make sure to pass `--api-key` argument or set `API_KEY` environment variable

**Issue**: "GOOGLE_TOKEN file not found"
- **Solution**: Pass `--google-token` with the JSON content or file path

**Issue**: MCP servers fail to start
- **Solution**: Ensure Node.js is working in the container (it's installed automatically). Check container logs for specific errors.

## Examples

### Complete Workflow

```bash
# 1. Build and push
QUAY_USERNAME=myuser ./container/build-and-push.sh

# 2. Run the container
podman run --rm \
  quay.io/myuser/vep-police-agent:latest \
  --api-key "$(cat ~/.config/gemini-api-key)" \
  --google-token "$(cat ~/.config/google-service-account.json)"
```

### CI/CD Integration

```bash
# Set credentials as environment variables
export QUAY_USERNAME=myuser
export QUAY_TOKEN=$(cat quay-token.txt)
export IMAGE_TAG=${CI_COMMIT_SHA:0:7}

# Login
echo "$QUAY_TOKEN" | podman login quay.io --username "$QUAY_USERNAME" --password-stdin

# Build and push
./container/build-and-push.sh
```

### Local Development

```bash
# Build locally without pushing
podman build -f container/Containerfile -t vep-agent:local .

# Run with local files
podman run --rm \
  -v $(pwd):/app:ro \
  vep-agent:local \
  --api-key "your-key" \
  --google-token "/app/GOOGLE_TOKEN"
```

## Security Notes

- **Never commit credentials** to version control
- **Use secrets management** in production (Kubernetes secrets, environment variables, etc.)
- **Rotate tokens regularly**
- **Use least-privilege service accounts** for Google Sheets access
- **Limit GitHub token permissions** to only what's needed

## Support

For issues or questions:
1. Check the main project README
2. Review container logs: `podman logs <container-id>`
3. Check application logs in the container output
