	"""
Vapi â†” IntakeQ integration server
-----------------------------------

This module exposes a simple Flask-based HTTP API that Vapi can call via a
function tool.  It wraps several IntakeQ endpoints and applies business
rules defined by the user:

* New clients must be scheduled for a 60â€‘minute psychiatric evaluation with
  either Charles Maddix or Ava Suleiman.  Dr. Raul Soto-Acosta does not
  accept new clients.
* Existing clients are scheduled with the same practitioner they visited
  during their most recent appointment.
* Providers have fixed weekly schedules (see `PROVIDER_SCHEDULES`) and
  appointments may not overlap lunch breaks.

The server exposes a single endpoint, `/tool-call`, which Vapi will
invoke via its function tool.  The request payload should contain a
``function_name`` indicating the desired action along with any required
parameters.  Supported functions include:

* ``lookup_client`` â€“Â Find a client record by phone number.
* ``lookup_appointments`` â€“Â Retrieve upcoming appointments for a client.
* ``create_appointment`` â€“Â Book a new appointment.
* ``reschedule_appointment`` â€“Â Change an existing appointment.
* ``cancel_appointment`` â€“Â Cancel an appointment.

Prior to running this server you must set the following environment
variables:

``INTAKEQ_API_KEY``
    Your IntakeQ API key.  It is used to authenticate requests to the
    IntakeQ REST API via the ``X-Auth-Key`` header.

``TIMEZONE``
    The IANA timezone identifier for your practice (e.g. ``America/New_York``).
    All date and time computations are performed using this timezone.

You can start the server locally for testing with ``python vapi_intakeq_server.py``.

Note: This server performs external API requests.  Before running it in
production, ensure you secure the endpoint (e.g. require an auth token
from Vapi) and avoid logging sensitive data. 
"""

import os
from datetime import datetime, timedelta, time
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request

try:
    # Python 3.9+ provides zoneinfo in the standard library
    from zoneinfo import ZoneInfo  # type: ignore
except ImportError:
    from pytz import timezone as ZoneInfo  # type: ignore


###############
# Configuration
###############

# Provider availability configuration.  The keys are internal identifiers
# matching practitioner names.  The value is a list of weekly schedules.
# Each schedule entry defines a weekday (0=Monday..6=Sunday), the start
# and end times (24 hour clock), and optional break intervals.  Lunch
# breaks are excluded from available slots.

# These definitions come from the user's description.  If schedules
# change in IntakeQ they should be updated here or ideally fetched from
# IntakeQ's scheduling settings.

PROVIDER_SCHEDULES: Dict[str, Dict[str, Any]] = {
    "Charles Maddix": {
        "practitioner_name": "Charles Maddix",
        "available": [
            {"weekday": 0, "start": time(10, 30), "end": time(18, 0), "breaks": [(time(13, 0), time(14, 0))]},
            {"weekday": 1, "start": time(10, 30), "end": time(18, 0), "breaks": [(time(13, 0), time(14, 0))]},
            {"weekday": 2, "start": time(10, 30), "end": time(18, 0), "breaks": [(time(13, 0), time(14, 0))]},
            {"weekday": 3, "start": time(10, 30), "end": time(18, 0), "breaks": [(time(13, 0), time(14, 0))]},
        ],
    },
    "Ava Suleiman": {
        "practitioner_name": "Ava Suleiman",
        "available": [
            {"weekday": 1, "start": time(10, 30), "end": time(18, 0), "breaks": [(time(13, 0), time(14, 0))]},
        ],
    },
    "Dr. Raul Soto-Acosta": {
        "practitioner_name": "Dr. Raul Soto-Acosta",
        "available": [
            {"weekday": 0, "start": time(16, 0), "end": time(18, 0), "breaks": []},
            {"weekday": 1, "start": time(16, 0), "end": time(18, 0), "breaks": []},
            {"weekday": 2, "start": time(16, 0), "end": time(18, 0), "breaks": []},
            {"weekday": 3, "start": time(16, 0), "end": time(18, 0), "breaks": []},
        ],
    },
}

# Duration for new client psychiatric evaluations (in minutes)
NEW_CLIENT_DURATION_MINUTES = 60

# The IntakeQ base URL for API requests
INTAKEQ_BASE_URL = "https://intakeq.com/api/v1"


#####################
# Helper functions
#####################

def get_api_key() -> str:
    """Return the IntakeQ API key from the environment.

    Raises an exception if the key is not set.  Separating this logic
    makes it easy to stub out during testing.
    """
    api_key = os.getenv("INTAKEQ_API_KEY")
    if not api_key:
        raise RuntimeError("INTAKEQ_API_KEY environment variable is not set")
    return api_key


def get_timezone() -> ZoneInfo:
    """Return the configured timezone for the practice."""
    tz_name = os.getenv("TIMEZONE", "America/New_York")
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # pragma: no cover - rarely triggered
        raise RuntimeError(f"Invalid timezone '{tz_name}': {exc}")


def auth_headers() -> Dict[str, str]:
    """Construct headers with the IntakeQ API key."""
    return {
        "X-Auth-Key": get_api_key(),
        "Content-Type": "application/json",
    }


def query_intakeq(path: str, method: str = "GET", params: Optional[Dict[str, Any]] = None,
                  data: Optional[Dict[str, Any]] = None) -> Any:
    """Send an HTTP request to the IntakeQ API.

    This helper centralizes error handling and authentication.  Raises
    ``requests.HTTPError`` on failure.
    """
    url = f"{INTAKEQ_BASE_URL}{path}"
    resp = requests.request(method, url, headers=auth_headers(), params=params, json=data)
    # Raise exception for 4xx/5xx status codes to surface errors immediately.
    resp.raise_for_status()
    # Attempt to decode JSON; some endpoints (e.g. PDF downloads) return binary.
    try:
        return resp.json()
    except ValueError:
        return resp.content


def get_booking_settings() -> Dict[str, Any]:
    """Retrieve services, locations and practitioners from IntakeQ.

    Returns a dictionary keyed by ``"Services"``, ``"Locations"``, and
    ``"Practitioners"``.  Each key maps to a list of dictionaries as
    described in the Appointment API docsã€107353462443215â€ L284-L320ã€‘.
    """
    return query_intakeq("/appointments/settings")


def find_practitioner_id_by_name(name: str, settings: Dict[str, Any]) -> Optional[str]:
    """Return the practitioner ID matching the provided full name.

    If no practitioner is found, returns ``None``.
    """
    for practitioner in settings.get("Practitioners", []):
        if practitioner.get("CompleteName", "").strip().lower() == name.lower():
            return practitioner.get("Id")
    return None


def find_service_id_by_name(name: str, settings: Dict[str, Any]) -> Optional[str]:
    """Return the service ID matching the provided name, or ``None`` if not found."""
    for service in settings.get("Services", []):
        if service.get("Name", "").strip().lower() == name.lower():
            return service.get("Id")
    return None


def find_location_id_by_name(name: str, settings: Dict[str, Any]) -> Optional[str]:
    """Return the location ID matching the provided name, or ``None`` if not found."""
    for location in settings.get("Locatons", []) + settings.get("Locations", []):
        # Both spellings occur in docs; IntakeQ uses "Locations" at times
        if location.get("Name", "").strip().lower() == name.lower():
            return location.get("Id")
    return None


def search_clients(search: str, include_profile: bool = False) -> List[Dict[str, Any]]:
    """Search for clients by name, email or phone.

    Returns a list of client objects.  Passing ``include_profile``
    yields full client profiles as described in the Client API docsã€558611595296568â€ L91-L104ã€‘.
    """
    params = {
        "search": search,
        "includeProfile": "true" if include_profile else "false",
    }
    return query_intakeq("/clients", params=params)


def get_client_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    """Retrieve a client record based on a phone number.

    Performs a partial search on the provided ``phone`` and returns the first
    matching client, or ``None`` if no client is found.
    """
    clients = search_clients(phone, include_profile=True)
    return clients[0] if clients else None


def query_appointments(client_id: Optional[int] = None, practitioner_email: Optional[str] = None,
                        start_date: Optional[str] = None, end_date: Optional[str] = None,
                        status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Query appointments filtered by client, practitioner and date range.

    Dates should be provided as ``YYYY-MM-DD`` in local time.  See the
    Appointment API docs for detailsã€107353462443215â€ L200-L276ã€‘.
    """
    params: Dict[str, Any] = {}
    if client_id is not None:
        params["clientId"] = client_id
    if practitioner_email:
        params["practitionerEmail"] = practitioner_email
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    if status:
        params["status"] = status
    return query_intakeq("/appointments", params=params)


def get_last_appointment(client_id: int) -> Optional[Dict[str, Any]]:
    """Return the most recent past appointment for a given client.

    The Appointment API returns results ordered by date descendingã€107353462443215â€ L200-L210ã€‘.
    We therefore fetch appointments with ``status`` absent (all statuses) and
    pick the first entry whose ``StartDate`` is in the past.
    """
    tz = get_timezone()
    now_local_date = datetime.now(tz).date()
    # Fetch all appointments for this client (max 100).  The API does not
    # provide an explicit filter for past vs future, so we check locally.
    appointments = query_appointments(client_id=client_id)
    for appt in appointments:
        # Convert StartDate (Unix ms) to local date
        try:
            start_ts = int(appt.get("StartDate", 0)) / 1000
            start_dt = datetime.fromtimestamp(start_ts, tz=tz)
        except Exception:
            continue
        if start_dt.date() <= now_local_date:
            return appt
    return None


def compute_available_slots(practitioner_name: str, duration_minutes: int,
                            days_ahead: int = 14) -> List[Tuple[datetime, datetime]]:
    """Calculate open appointment slots for a practitioner.

    This function walks through the provider's weekly schedule and subtracts
    existing appointments to determine available windows.  It returns a
    sorted list of tuples ``(start, end)`` as ``datetime`` objects in the
    practice's local timezone.

    Args:
        practitioner_name: Full practitioner name as defined in
            ``PROVIDER_SCHEDULES``.  Must exist in IntakeQ's settings.
        duration_minutes: Desired appointment length.
        days_ahead: How many days into the future to search.

    Returns:
        A list of available (start, end) slots.

    Note:
        This computation is only approximate; it does not account for
        appointments booked after this function is called.  It also
        assumes the practitioner has only one location.  If multiple
        locations exist, modify logic accordingly.
    """
    tz = get_timezone()
    # Gather provider schedule definition
    provider_info = PROVIDER_SCHEDULES.get(practitioner_name)
    if not provider_info:
        return []
    schedule = provider_info.get("available", [])
    # Retrieve practitioner email from IntakeQ settings for appointment queries
    settings = get_booking_settings()
    practitioner_email = None
    for practitioner in settings.get("Practitioners", []):
        if practitioner.get("CompleteName", "").strip().lower() == practitioner_name.lower():
            practitioner_email = practitioner.get("Email")
            break
    if not practitioner_email:
        return []
    # Fetch upcoming appointments for this practitioner in the date range
    today = datetime.now(tz).date()
    end_date = (today + timedelta(days=days_ahead)).isoformat()
    appts = query_appointments(practitioner_email=practitioner_email,
                               start_date=today.isoformat(),
                               end_date=end_date)
    # Build a dictionary keyed by date -> list of (start, end) times
    busy_map: Dict[datetime.date, List[Tuple[datetime, datetime]]] = {}
    for appt in appts:
        try:
            start_ts = int(appt.get("StartDate")) / 1000
            end_ts = int(appt.get("EndDate")) / 1000
        except Exception:
            continue
        start_local = datetime.fromtimestamp(start_ts, tz=tz)
        end_local = datetime.fromtimestamp(end_ts, tz=tz)
        busy_map.setdefault(start_local.date(), []).append((start_local, end_local))
    # Compute available slots for each day in the range
    available_slots: List[Tuple[datetime, datetime]] = []
    for day_offset in range(days_ahead + 1):
        day_date = today + timedelta(days=day_offset)
        weekday = day_date.weekday()
        # Find matching schedule entries for this weekday
        for sched in schedule:
            if sched["weekday"] != weekday:
                continue
            day_start = datetime.combine(day_date, sched["start"], tz)
            day_end = datetime.combine(day_date, sched["end"], tz)
            # Build a list of busy intervals, including breaks
            busy_intervals: List[Tuple[datetime, datetime]] = []
            # Add lunch and other breaks
            for br_start, br_end in sched.get("breaks", []):
                busy_intervals.append((datetime.combine(day_date, br_start, tz),
                                        datetime.combine(day_date, br_end, tz)))
            # Add existing appointments
            for appt_start, appt_end in busy_map.get(day_date, []):
                busy_intervals.append((appt_start, appt_end))
            # Sort busy intervals
            busy_intervals.sort(key=lambda x: x[0])
            # Walk through the day and find free intervals of sufficient length
            current_start = day_start
            for busy_start, busy_end in busy_intervals:
                if busy_start - current_start >= timedelta(minutes=duration_minutes):
                    available_slots.append((current_start, busy_start))
                # Move current_start forward if busy_end is later
                if busy_end > current_start:
                    current_start = busy_end
            # Check slot at end of day
            if day_end - current_start >= timedelta(minutes=duration_minutes):
                available_slots.append((current_start, day_end))
    # Flatten slots into durations matching exactly the appointment length
    final_slots: List[Tuple[datetime, datetime]] = []
    for start, end in available_slots:
        slot_start = start
        while slot_start + timedelta(minutes=duration_minutes) <= end:
            slot_end = slot_start + timedelta(minutes=duration_minutes)
            final_slots.append((slot_start, slot_end))
            # Move to next slot; optionally enforce buffer time here (none specified)
            slot_start = slot_end
    # Sort by start time
    final_slots.sort(key=lambda x: x[0])
    return final_slots


def create_intakeq_appointment(practitioner_id: str, client_id: int, service_id: str,
                               location_id: str, start_time: datetime) -> Dict[str, Any]:
    """Create a new appointment in IntakeQ.

    Converts ``start_time`` in local timezone to a UTC timestamp in milliseconds
    as required by IntakeQã€107353462443215â€ L321-L334ã€‘.  Returns the appointment
    object on success.
    """
    tz = get_timezone()
    # Convert to UTC
    start_utc = start_time.astimezone(datetime.timezone.utc)
    # Build payload
    payload = {
        "PractitionerId": practitioner_id,
        "ClientId": client_id,
        "ServiceId": service_id,
        "LocationId": location_id,
        "Status": "Confirmed",
        "UtcDateTime": int(start_utc.timestamp() * 1000),
        "SendClientEmailNotification": True,
        "ReminderType": "Sms",
    }
    return query_intakeq("/appointments", method="POST", data=payload)


def reschedule_intakeq_appointment(appointment_id: str, new_start: datetime) -> Dict[str, Any]:
    """Reschedule an existing appointment by updating its start time.

    Returns the updated appointment object on success.
    """
    tz = get_timezone()
    start_utc = new_start.astimezone(datetime.timezone.utc)
    payload = {
        "Id": appointment_id,
        "UtcDateTime": int(start_utc.timestamp() * 1000),
    }
    return query_intakeq("/appointments", method="PUT", data=payload)


def cancel_intakeq_appointment(appointment_id: str, reason: str = "Canceled by client") -> Dict[str, Any]:
    """Cancel an appointment via the IntakeQ API.

    Returns the canceled appointment object on success.
    """
    payload = {
        "AppointmentId": appointment_id,
        "Reason": reason,
    }
    return query_intakeq("/appointments/cancellation", method="POST", data=payload)


###################
# Flask application
###################

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health() -> Any:
    """Simple healthcheck endpoint for monitoring."""
    return jsonify({"status": "ok"})


@app.route("/tool-call", methods=["POST"])
def tool_call() -> Any:
    """Handle tool calls from Vapi.

    The request should contain a JSON body with the fields:

    ``function_name`` â€“ the name of the function to call
    ``parameters`` â€“ a dictionary of parameters expected by that function

    Based on ``function_name`` we dispatch to the appropriate helper.  Any
    exceptions result in a 400/500 response with an error message.
    """
    payload = request.get_json(silent=True) or {}
    function_name = payload.get("function_name")
    params = payload.get("parameters", {})
    try:
        if function_name == "lookup_client":
            phone = params.get("phone")
            if not phone:
                return jsonify({"error": "Missing parameter: phone"}), 400
            client = get_client_by_phone(phone)
            return jsonify({"client": client})
        elif function_name == "lookup_appointments":
            client_id = params.get("client_id")
            if client_id is None:
                return jsonify({"error": "Missing parameter: client_id"}), 400
            appointments = query_appointments(client_id=int(client_id))
            return jsonify({"appointments": appointments})
        elif function_name == "create_appointment":
            # For new clients and for existing clients booking a followâ€‘up
            client_id = params.get("client_id")
            practitioner_name = params.get("practitioner_name")
            service_name = params.get("service_name")
            location_name = params.get("location_name", "Main Office")
            start_iso = params.get("start_iso")
            if not all([client_id, practitioner_name, service_name, start_iso]):
                return jsonify({"error": "Missing one or more parameters"}), 400
            settings = get_booking_settings()
            practitioner_id = find_practitioner_id_by_name(practitioner_name, settings)
            service_id = find_service_id_by_name(service_name, settings)
            location_id = find_location_id_by_name(location_name, settings)
            if not practitioner_id or not service_id or not location_id:
                return jsonify({"error": "Invalid practitioner/service/location"}), 400
            # Parse start time as ISO in local tz
            tz = get_timezone()
            start_time = datetime.fromisoformat(start_iso).replace(tzinfo=tz)
            appointment = create_intakeq_appointment(practitioner_id, int(client_id), service_id,
                                                     location_id, start_time)
            return jsonify({"appointment": appointment})
        elif function_name == "reschedule_appointment":
            appointment_id = params.get("appointment_id")
            new_start_iso = params.get("new_start_iso")
            if not appointment_id or not new_start_iso:
                return jsonify({"error": "Missing parameter: appointment_id or new_start_iso"}), 400
            tz = get_timezone()
            new_start = datetime.fromisoformat(new_start_iso).replace(tzinfo=tz)
            updated = reschedule_intakeq_appointment(appointment_id, new_start)
            return jsonify({"appointment": updated})
        elif function_name == "cancel_appointment":
            appointment_id = params.get("appointment_id")
            reason = params.get("reason", "Canceled by client")
            if not appointment_id:
                return jsonify({"error": "Missing parameter: appointment_id"}), 400
            canceled = cancel_intakeq_appointment(appointment_id, reason)
            return jsonify({"appointment": canceled})
        elif function_name == "get_available_slots":
            practitioner_name = params.get("practitioner_name")
            duration = params.get("duration_minutes", NEW_CLIENT_DURATION_MINUTES)
            if not practitioner_name:
                return jsonify({"error": "Missing parameter: practitioner_name"}), 400
            slots = compute_available_slots(practitioner_name, int(duration))
            # Return ISO strings for easier consumption by the LLM
            slot_list = [
                {"start_iso": s.isoformat(), "end_iso": e.isoformat()}
                for s, e in slots
            ]
            return jsonify({"available_slots": slot_list})
        else:
            return jsonify({"error": f"Unknown function '{function_name}'"}), 400
    except requests.HTTPError as e:
        # Surface IntakeQ errors cleanly
        try:
            error_json = e.response.json()
        except Exception:
            error_json = {"status": e.response.status_code, "message": e.response.text}
        return jsonify({"error": "IntakeQ API error", "details": error_json}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Allow the port to be set via the environment for flexible deployment
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
