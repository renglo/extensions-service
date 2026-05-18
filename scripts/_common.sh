# Shared setup for extensions-service shell scripts.
# Disable AWS CLI pager so long JSON output does not block the terminal in less.
export AWS_PAGER="${AWS_PAGER:-}"
