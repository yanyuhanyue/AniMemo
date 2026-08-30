function nibble(value) {
  value = toupper(value)
  return index("0123456789ABCDEF", value) - 1
}

function hex(value, result, position, digit) {
  result = 0
  for (position = 1; position <= length(value); position += 1) {
    digit = nibble(substr(value, position, 1))
    if (digit < 0) {
      return -1
    }
    result = (result * 16) + digit
  }
  return result
}

function octet(value) {
  return hex(value)
}

$2 == "00000000" && $3 ~ /^[[:xdigit:]]+$/ && length($3) == 8 && \
    $4 ~ /^[[:xdigit:]]+$/ && length($4) <= 8 && $8 == "00000000" {
  flags = hex($4)
  if (flags >= 0 && (flags % 2) == 1 && (int(flags / 2) % 2) == 1) {
    count += 1
    gateway = octet(substr($3, 7, 2)) "." octet(substr($3, 5, 2)) "." \
      octet(substr($3, 3, 2)) "." octet(substr($3, 1, 2))
  }
}

END {
  if (count != 1) {
    exit 1
  }
  print gateway
}
