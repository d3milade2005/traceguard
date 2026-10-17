ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE_ID
ARG SUITE_DIGEST
LABEL org.traceguard.evaluation-base=$BASE_IMAGE_ID \
      org.traceguard.evaluation-bundle=$SUITE_DIGEST
COPY evaluation/__init__.py evaluation/harness.py /opt/runtime/evaluation/
COPY app/integrations/execution/sandbox/conftest.py /opt/runtime/app/integrations/execution/sandbox/conftest.py
COPY evaluation/sandbox/conftest.py /opt/runner/conftest.py
