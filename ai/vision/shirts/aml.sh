#!/bin/sh
# Trains the shirt-number model on Azure Machine Learning (job.yml), registers it as the model
# shirt-numbers, and downloads it to ai/vision/models/shirt_numbers.onnx.
#
#   ai/vision/shirts/aml.sh [resource group] [workspace]    (default: deploy/azure's outputs)
#   EPOCHS=2 PER_EPOCH=2000 ai/vision/shirts/aml.sh          (a quick run, to try the setup)
set -eu
cd "$(dirname "$0")/.."                                      # ai/vision: the job's code
TF=../../deploy/azure
RG=${1:-$(terraform -chdir=$TF output -raw resource_group)}
WS=${2:-$(terraform -chdir=$TF output -raw ml_workspace)}
ml="-g $RG -w $WS"

az extension show -n ml >/dev/null 2>&1 || az extension add -n ml -y --only-show-errors
set -- --set "inputs.epochs=${EPOCHS:-16}" --set "inputs.per_epoch=${PER_EPOCH:-24000}"
job=$(az ml job create -f shirts/job.yml $ml "$@" --query name -o tsv)
echo "job $job: $(az ml job show -n "$job" $ml --query services.Studio.endpoint -o tsv)"
az ml job stream -n "$job" $ml
status=$(az ml job show -n "$job" $ml --query status -o tsv)
[ "$status" = Completed ] || { echo "job $job: $status" >&2; exit 1; }

version=$(az ml model create --name shirt-numbers --type custom_model --path "azureml://jobs/$job/outputs/model" \
    --description "Shirt numbers on synthetic players (job $job)" --tags data=synthetic $ml --query version -o tsv)
rm -rf "models/shirt-numbers-$version"
az ml model download --name shirt-numbers --version "$version" --download-path "models/shirt-numbers-$version" $ml
onnx=$(find "models/shirt-numbers-$version" -name shirt_numbers.onnx | head -n 1)
cp "$onnx" models/shirt_numbers.onnx
find "models/shirt-numbers-$version" -name metrics.json -exec cat {} \;
echo "→ ai/vision/models/shirt_numbers.onnx (shirt-numbers:$version). Compare it with the vision-language model:"
echo "  python -m matchvision.numbers_eval compare <labelled crops> --onnx models/shirt_numbers.onnx --ollama-model gemma4:e2b"
