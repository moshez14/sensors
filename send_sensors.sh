#!/bin/bash

FILE="mishmar.csv"

while read -r col1 col2 col3 col4 lat lon idx; do
  # Extract the number (XX) from התראת_קטע_XX
  num=$(echo "$col1" | sed -E 's/.*_([0-9]+)/\1/')

  name="$num קטע"

  echo "curl -X POST http://localhost:5500/add_sensor \
  -H \"Content-Type: application/json\" \
  -d '{
    \"name\": \"$name\",
    \"nameOnTheMap\": \"$name\",
    \"latitude\": $lat,
    \"longitude\": $lon,
    \"createdBy\": \"69c119759203ccbe0f5f08e1\"
  }'"

done < "$FILE"
