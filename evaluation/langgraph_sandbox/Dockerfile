ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE_ID
ARG REPLAY_DIGEST
LABEL org.traceguard.langgraph-base=$BASE_IMAGE_ID \
      org.traceguard.langgraph-bundle=$REPLAY_DIGEST
USER root
COPY evaluation/langgraph_sandbox/requirements.lock /opt/runner/langgraph-requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/runner/langgraph-requirements.lock
COPY evaluation/__init__.py /opt/runtime/evaluation/__init__.py
COPY evaluation/langgraph_sandbox/harness.py /opt/runtime/evaluation/langgraph_sandbox/harness.py
COPY examples/langgraph_refund/application.py examples/langgraph_refund/integration.py /opt/runtime/examples/langgraph_refund/
COPY app/integrations/execution/sandbox/conftest.py /opt/runtime/app/integrations/execution/sandbox/conftest.py
COPY evaluation/langgraph_sandbox/conftest.py /opt/runner/conftest.py
USER 10001:10001
