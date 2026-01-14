#!/bin/bash

podman run --rm \
    quay.io/mabekitzur/vep-police-agent:latest \
    --api-key "$(cat API_KEY)" \
    --google-token "$(cat GOOGLE_TOKEN)"


