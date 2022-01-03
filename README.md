A hack of [doctoshotgun](https://github.com/rbignon/doctoshotgun)
that provides another useful `docto_alert_on_new_slot.py` script,
to be **notified when a new slot is available for a given consultation type at your doctor**:

    ./docto_alert_on_new_slot.py doctor-name $EMAIL $PASSWORD

Where `doctor-name` is the ID of the doctor,
as it appears in their DoctoLib page URL,
usually _firstname-lastname_.

By default, you will be prompted to choose the consultation type.
Its ID will be displayed, and you can provide it directly to the script with `--motive-id`.

The script will loop forever, and starts beeping once it has detected a new available slot.

Country is always set to _France_.
