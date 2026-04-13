#!/bin/zsh

cd "/Users/eyal.boumgarten/Documents/Projects/AI Usage" || exit 1

python3 report_picker.py

exit_code=$?
echo
if [ $exit_code -eq 0 ]; then
  echo "Report generation finished successfully."
else
  echo "Report generation exited with status $exit_code."
fi
echo "Press Return to close this window."
read
