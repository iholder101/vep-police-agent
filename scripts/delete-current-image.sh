#!/bin/bash

podman rmi -f $(podman images -q --filter "reference=vep-police-agent")

