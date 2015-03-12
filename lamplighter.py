#!/usr/bin/env python

"""Lamplighter: Automated light management.

Lamplighter will periodically search the network for specific MAC
addresses. If the MAC addresses are not found, it will switch off
the lights. It does the inverse when at least one MAC address
appears."""

import os
import sys
import time
import datetime
import subprocess
import requests
import urllib
import signal
from ConfigParser import SafeConfigParser

import config

def main():
    create_pidfile()
    config.load()
    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGPOLL, handle_poll)
    
    if not within_quiet_hours():
        send_message('Lamplighter online.')

    while True:
        search()
        time.sleep(1)
        
def search():
    """The main thread."""
    
    quiet_hours = ""
    if within_quiet_hours():
        quiet_hours = " (quiet hours)"
    log("--- Commencing search%s ---" % quiet_hours)

    state = current_state()
    if state == False:
        log("No current state. Initializing.")

        if phone_count is 0:
            log("Current state: away.")
            state = "away"
        else:
            log("Current state: home.")
            state = "home"

        save_state(state)
        
    log("Current state is %s." % state)

    phone_count = False
    while phone_count is False:
        log("Finding initial phone count...")
        phone_count = count_phones_present()
        
    # Either due to wireless network blips or general unreliability of
    # a single network scan, these scans are guaranteed to be correct
    # about finding any given device, but also very likely to be
    # incorrect about *not finding* devices. What this means is that a
    # transition from "away" to "home" can be done with confidence; if
    # any device is seen on the network, we can be certain that it is
    # real. However, transitions from "home" to "away" must be done
    # more cautiously, lest you have all of the lights in your house
    # turn off and back on repeatedly while you're there (which
    # happened to me, a lot).
    #
    # This seems to be a fairly good compromise between accuracy and
    # complexity: if it looks like no devices were found, wait ten
    # seconds and then scan for them three more times, waiting for
    # five seconds between each scan. If all three of those scans find
    # nothing, we'll commit to the state change.
    if state == "home" and phone_count is 0:
        # Delay ten seconds and then check three more times.
        log("*** Possible change to away; wait 10 sec. and search 3 more times...")
        time.sleep(10)

        if confirm_phone_count():
            log("Phones confirmed missing. State changed to away.")
            save_state("away")

            if within_quiet_hours():
                log("Within quiet hours! Simply notifying.")
                send_message("It appears you've left, but it's late, and I'm tired. Turn off your own lights.")
            else:
                lights_off()
                send_message("Lights are now off. Have a good day.")

    elif state == "away" and phone_count > 0:
        log("*** State changing to home.")
        save_state("home")

        if within_quiet_hours():
            log("Within quiet hours! Simply notifying.")
            send_message("It appers you've returned home, but it's late, and I have a headache. Go to bed.")
        else:
            lights_on()
            send_message("Lights are now on. Welcome home.")
        
    else:
        log("State is '%s', phone count is %s; nothing to do." % (state, phone_count))

def within_quiet_hours():
    now = datetime.datetime.now()
    
    # The config module does not know nor care what the values within
    # the config file are, nor their types. We'll get strings for
    # everything, so coerce them into ints so we can compare them.
    start = int(config.config['quiet_hours_start'])
    end = int(config.config['quiet_hours_end'])
    
    if start is 0 and end is 0:
        return False

    # Quiet hours is a range within the same day.
    if start < end and \
       start <= now.hour and end > now.hour:
        return True

    # Quiet hours is a range spanning a day change (through midnight).
    if start > end and \
       (start <= now.hour or end > now.hour):
        return True
        
def confirm_phone_count():
    log("*** Performing 3 confirmation searches...")

    for x in range(3):
        test = count_phones_present()
        log("*** Found %s phone(s)." % test)

        if test is not 0:
            log("*** False alarm, phone(s) found.")
            return False

        time.sleep(5)

    return True

def log(message):
    """Output a pretty log message."""
    pid = os.getpid()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    print "[%s] %s: %s" % (pid, now, message)

def state_file_path():
    """Return the path to the state file."""
    return  "/tmp/welcome_home_state"

def save_state(state):
    """Save the given state to disk."""
    statefile_name = state_file_path()
    if os.path.isfile(statefile_name):
        os.unlink(statefile_name)
    file(statefile_name, "w").write(state)

def current_state():
    """Find the current (last saved) state."""
    statefile_name = state_file_path()
    if os.path.isfile(statefile_name):
        statefile = open(statefile_name, "r")
        statefile.seek(0)
        state = statefile.readline().rstrip("\n")
        return state
    else:
        return False

def get_pidfile_name():
    """Return the name of our pid file."""
    return str("/var/run/lamplighter.pid")

def handle_poll(signum, frame):
    log("Received SIGPOLL, reloading config file.")
    config.load()

def handle_term(signum, frame):
    """Clean up and exit."""
    log("Received SIGTERM; cleaning up and exiting.")
    os.unlink(get_pidfile_name())
    sys.exit(0)

def create_pidfile():
    """Create a pidfile for this process."""
    pid = str(os.getpid())
    log("Creating pidfile for %s" % pid)
    file(get_pidfile_name(), "w").write(pid)

def count_phones_present():
    """Count the known phones on the network."""
    log("Searching for phones...")
    try:
        count = 0
        phone_search = subprocess.check_output(["sudo",
                                                "nmap",
                                                "-sn",
                                                "-n",
                                                "-T5",
                                                "192.168.10.50-255"])

        if phone_search.find("34:4D:F7:0E:C7:5B") > -1:
            log("Found Aaron's phone.")
            count += 1

        if phone_search.find("FC:C2:DE:58:99:AD") > -1:
            log("Found Veronica's phone.")
            count += 1

        return count
    except subprocess.CalledProcessError:
        # Grep returns a non-zero exit status when nothing is found.
        log("Search returned non-zero exit status.")
        return False

def lights_on():
    """Turn the lights on."""
    r = requests.post("http://lights.skynet.net/scenes/by_name/Relax")

def lights_off():
    """Turn the lights off."""
    r = requests.post("http://lights.skynet.net/scenes/by_name/All+Off")

def send_message(message):
    """Send a text message to my phone through Twilio."""
    url = "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % config.config['twilio_account_id']
    payload = {
        'Body': message,
        'From': config.config['twilio_outgoing_number'],
    }
    creds = (config.config['twilio_account_id'], config.config['twilio_auth_token'])

    numbers = config.config['twilio_notification_numbers']
    for number in numbers.split(","):
        payload['To'] = number
        response = requests.post(url, data=payload, auth=creds)
        rsdata = response.json()

        log("Message %s to %s '%s' at %s." % (rsdata['sid'],
                                            rsdata['to'],
                                            rsdata['status'],
                                            rsdata['date_created']))

if __name__ == "__main__":
    main()
