# The pipeline's five stages as one image, for an unattended run on a machine with Docker.
#
# The base image is the release of Python that mise.toml pins, named by the digest of its index
# of platforms, because Docker Hub moves a tag such as 3.14-slim to every new release and every
# rebuild of the image. With the digest, every build of a commit starts from the same interpreter
# and the same pip. test_tech_stack.py fails when the tag names another release than mise.toml,
# and install_tools.sh fails the build when the interpreter of the image is another release, as
# when the tag changed and the digest, which Docker pulls without reading the tag, did not.
# To move to a new release, change mise.toml and this line together, with the digest that
# `docker buildx imagetools inspect python:<release>-slim` prints.
#
# The image contains the code at /opt/pipeline and no data: the package, the scripts of the
# sampling, classification and analysis stages, run_pipeline.py, the heuristics, mise.toml,
# which pins the interpreter, git, rsync and 7-Zip, install_tools.sh, which installed those
# three tools, the dependency locks of the Python distributions, and the entrypoint. Everything
# that a run reads and writes is in the data root, mounted from the project folder. The build
# creates the two mount points under /app, and at every start entrypoint.py runs the image's
# code over the mounted data root. Its docstring explains why the data root is the volume.
FROM python:3.14.10-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
ARG GIT_COMMIT=unrecorded
ARG BUILD_DATE=unrecorded

# git for the clones, rsync for the copy out of each clone, 7zz for the release archives and the
# handover. These are BINARY_REQUIREMENTS of the runner, at the versions that mise.toml pins and
# every development machine runs: conda-forge's packages of git and rsync, and the `7zz` of the
# 7-Zip release, which install_tools.sh installs. The cloning step takes over a repository only
# from a collection under the same `git --version`, so an image with another git collects the
# whole sample again, and a new pin of git in mise.toml is a decision to collect again.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY mise.toml deploy/install_tools.sh /tmp/tools/
RUN sh /tmp/tools/install_tools.sh /tmp/tools && rm -rf /tmp/tools

# Python writes no .pyc files beside the modules that it imports, and it writes its output
# unbuffered, so the container log shows each line as soon as it is written.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# The Python distributions of the five stages, from the dependency lock locks/pipeline.txt, which
# lists each with its exact version and the hashes of its files, at the versions that CI and
# every development machine install (locks/0_README.md). With --require-hashes, pip refuses a
# distribution that the dependency lock does not list. setuptools comes first, from
# locks/build.txt, and builds venn, which PyPI has as a source distribution only, and the
# editable package (--no-build-isolation), so that no build downloads a setuptools that no
# dependency lock lists. The image installs into the interpreter of the base image and has no
# virtual environment, because the image is the environment.
COPY locks/build.txt locks/pipeline.txt /opt/pipeline/locks/
RUN pip install --no-cache-dir --require-hashes -r /opt/pipeline/locks/build.txt \
 && pip install --no-cache-dir --require-hashes --no-build-isolation \
      -r /opt/pipeline/locks/pipeline.txt

COPY . /opt/pipeline/
# The `agentcontext` package under /opt/pipeline, installed editable so that the scripts of
# every stage import it from wherever they run.
RUN pip install --no-cache-dir --no-deps --no-build-isolation -e /opt/pipeline
RUN printf 'commit %s\nbuilt %s\npython %s\n' "$GIT_COMMIT" "$BUILD_DATE" \
      "$(python3 -c 'import platform; print(platform.python_version())')" \
      > /opt/pipeline/BUILD_INFO \
 && mkdir -p /app/data /app/handover

WORKDIR /app/data
ENTRYPOINT ["python3", "/opt/pipeline/deploy/entrypoint.py"]
# What a bare start runs: the five stages, with commit reclassification after collection and
# the two checks over the commit CSVs of the cloning step. Everything after the image name
# is passed to `run_pipeline.py`.
CMD ["--with-commit-reclassification"]
