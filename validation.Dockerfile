FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
ARG BUNDLE_DIGEST
LABEL org.traceguard.runtime="regression-sandbox-v1" \
      org.traceguard.bundle=$BUNDLE_DIGEST
COPY app/integrations/execution/sandbox/requirements.lock /opt/runner/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/runner/requirements.lock
RUN mkdir -p /opt/runtime/app/integrations/execution \
    && touch /opt/runtime/app/__init__.py /opt/runtime/app/integrations/__init__.py /opt/runtime/app/integrations/execution/__init__.py
COPY app/regression_runtime.py /opt/runtime/app/regression_runtime.py
COPY app/integrations/execution/calculator.py /opt/runtime/app/integrations/execution/calculator.py
COPY app/integrations/execution/refund.py /opt/runtime/app/integrations/execution/refund.py
COPY app/integrations/execution/langgraph_refund.py /opt/runtime/app/integrations/execution/langgraph_refund.py
COPY app/integrations/execution/sandbox/conftest.py app/integrations/execution/sandbox/entrypoint.py /opt/runner/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "/opt/runner/entrypoint.py"]
