#!/usr/bin/env python3
import argparse, datetime, getpass, json, logging, os, re, sys, tempfile, unicodedata
from pathlib import Path
from time import sleep

import cloudscraper
import colorama
from termcolor import colored

from woob.browser.exceptions import ClientError, ServerError, HTTPNotFound
from woob.browser.browsers import LoginBrowser, StatesMixin
from woob.browser.url import URL
from woob.browser.pages import JsonPage
from woob.tools.log import createColoredFormatter

try:
    from playsound import playsound as _playsound, PlaysoundException
    def playsound(*args):
        try:
            _playsound(*args)
        except (PlaysoundException, ModuleNotFoundError):
            pass  # do not crash if, for one reason or another, something wrong happens
except ImportError:
    from subprocess import Popen
    def playsound(file_path):  # launch process in the background (do not wait for it to complete)
        Popen(['/c/Program Files/VideoLAN/VLC/vlc.exe', file_path])


def log(text, *args, **kwargs):
    args = (colored(arg, 'yellow') for arg in args)
    if 'color' in kwargs:
        text = colored(text, kwargs.pop('color'))
    text = text % tuple(args)
    print(text, **kwargs)


class Session(cloudscraper.CloudScraper):
    def send(self, *args, **kwargs):
        callback = kwargs.pop('callback', lambda future, response: response)
        is_async = kwargs.pop('is_async', False)

        if is_async:
            raise ValueError('Async requests are not supported')

        resp = super().send(*args, **kwargs)

        return callback(self, resp)


class LoginPage(JsonPage):
    def redirect(self):
        return self.doc['redirection']


class SendAuthCodePage(JsonPage):
    def build_doc(self, text):
        return ""  # Do not choke on empty response from server


class ChallengePage(JsonPage):
    def build_doc(self, text):
        return ""  # Do not choke on empty response from server


class DoctorBookingPage(JsonPage):
    def get_places(self):
        return self.doc['data']['places']

    def get_practice(self):
        return self.doc['data']['places'][0]['practice_ids'][0]

    def get_agenda_ids(self, motive_id, practice_id=None):
        agenda_ids = []
        for a in self.doc['data']['agendas']:
            if motive_id in a['visit_motive_ids'] and \
               not a['booking_disabled'] and \
               (not practice_id or a['practice_id'] == practice_id):
                agenda_ids.append(str(a['id']))

        return agenda_ids

    def get_profile_id(self):
        return self.doc['data']['profile']['id']


class AvailabilitiesPage(JsonPage):
    pass


class MasterPatientPage(JsonPage):
    def get_patients(self):
        return self.doc

    def get_name(self):
        patient = self.doc[0]
        return f"{patient['first_name']}-{patient['last_name']}"


class Doctolib(LoginBrowser, StatesMixin):
    # individual properties for each country. To be defined in subclasses
    BASEURL = ""
    vaccine_motives = {}
    centers = URL('')
    center = URL('')
    # common properties
    login = URL('/login.json', LoginPage)
    send_auth_code = URL('/api/accounts/send_auth_code', SendAuthCodePage)
    challenge = URL('/login/challenge', ChallengePage)
    doctor_booking = URL(r'/booking/(?P<doctor_id>.+).json', DoctorBookingPage)
    availabilities = URL(r'/availabilities.json', AvailabilitiesPage)
    master_patient = URL(r'/account/master_patients.json', MasterPatientPage)

    def _setup_session(self, profile):
        session = Session()

        session.hooks['response'].append(self.set_normalized_url)
        if self.responses_dirname is not None:
            session.hooks['response'].append(self.save_response)

        self.session = session

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session.headers['sec-fetch-dest'] = 'document'
        self.session.headers['sec-fetch-mode'] = 'navigate'
        self.session.headers['sec-fetch-site'] = 'same-origin'
        self.session.headers['User-Agent'] = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.114 Safari/537.36'

        self.patient = None

    def locate_browser(self, state):
        # When loading state, do not locate browser on the last url.
        pass

    def do_login(self):
        try:
            self.open(self.BASEURL + '/sessions/new')
        except ServerError as e:
            if e.response.status_code in [503] \
                and 'text/html' in e.response.headers['Content-Type'] \
                    and ('cloudflare' in e.response.text or 'Checking your browser before accessing' in e .response.text):
                log('Request blocked by CloudFlare', color='red')
            if e.response.status_code in [520]:
                log('Cloudflare is unable to connect to Doctolib server. Please retry later.', color='red')
            raise
        try:
            self.login.go(json={'kind': 'patient',
                                'username': self.username,
                                'password': self.password,
                                'remember': True,
                                'remember_username': True})
        except ClientError:
            print('Wrong login/password')
            return False

        if self.page.redirect() == "/sessions/two-factor":
            print("Requesting 2fa code...")
            if not sys.__stdin__.isatty():
                log("Auth Code input required, but no interactive terminal available. Please provide it via command line argument '--code'.", color='red')
                return False
            self.send_auth_code.go(
                json={'two_factor_auth_method': 'email'}, method="POST")
            code = input("Enter auth code: ")
            try:
                self.challenge.go(
                    json={'auth_code': code, 'two_factor_auth_method': 'email'}, method="POST")
            except HTTPNotFound:
                print("Invalid auth code")
                return False

        return True

    def get_patients(self):
        self.master_patient.go()
        return self.page.get_patients()

    @classmethod
    def normalize(cls, string):
        nfkd = unicodedata.normalize('NFKD', string)
        normalized = ''.join(
            [c for c in nfkd if not unicodedata.combining(c)])
        normalized = re.sub(r'\W', '-', normalized)
        return normalized.lower()

    def has_availability(self, args, start_date):
        doctor_page = self.doctor_booking.go(doctor_id=args.doctor_id)
        profile_id = self.page.get_profile_id()
        practice_id = self.page.get_practice()
        if not args.motive_id:
            visit_motives = self.page.doc['data']['visit_motives']
            if len(visit_motives) == 0:
                raise EnvironmentError('Doctor does not offer any consultation ATM')
            if len(visit_motives) == 1:
                args.motive_id = visit_motives[0]['id']
            else:
                print('Available motives are:')
                for i, visit_motive in enumerate(visit_motives):
                    print('* [%s] %s (ID: %d)' % (i, visit_motive['name'], visit_motive['id']))
                while True:
                    print('What is your consultation motive?', end=' ', flush=True)
                    try:
                        args.motive_id = visit_motives[int(sys.stdin.readline().strip())]['id']
                    except (ValueError, IndexError):
                        continue
                    else:
                        break
        agenda_ids = doctor_page.get_agenda_ids(args.motive_id, practice_id)
        logging.debug(f"{profile_id=} {practice_id=} {agenda_ids=}")

        date = start_date.strftime('%Y-%m-%d')
        while date is not None:
            self.availabilities.go(
                params={'start_date': date,
                        'visit_motive_ids': args.motive_id,
                        'agenda_ids': '-'.join(agenda_ids),
                        'insurance_sector': 'public',
                        'practice_ids': practice_id,
                        'destroy_temporary': 'true',
                        'limit': 3})
            if 'next_slot' in self.page.doc:
                date = self.page.doc['next_slot']
            else:
                date = None

        log(self.page.doc.get('message', ''))
        availabilities = self.page.doc['availabilities']
        if not availabilities or not any(a['slots'] for a in availabilities):
            log('no availabilities', color='red')
            return False
        log('availabilities: %s', availabilities, color='green')
        return True


class DoctolibFR(Doctolib):
    BASEURL = 'https://www.doctolib.fr'


class Application:
    DATA_DIRNAME = (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")) / 'doctoshotgun'
    STATE_FILENAME = DATA_DIRNAME / 'state.json'

    @classmethod
    def create_default_logger(cls):
        # stderr logger
        format = '%(asctime)s:%(levelname)s:%(name)s:' \
                 ':%(filename)s:%(lineno)d:%(funcName)s %(message)s'
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(createColoredFormatter(sys.stderr, format))
        return handler

    def setup_loggers(self, level):
        logging.root.handlers = []

        logging.root.setLevel(level)
        logging.root.addHandler(self.create_default_logger())

    def load_state(self):
        try:
            with open(self.STATE_FILENAME, 'r') as fp:
                state = json.load(fp)
        except IOError:
            return {}
        else:
            return state

    def save_state(self, state):
        if not os.path.exists(self.DATA_DIRNAME):
            os.makedirs(self.DATA_DIRNAME)
        with open(self.STATE_FILENAME, 'w') as fp:
            json.dump(state, fp)

    def main(self, cli_args=None):
        colorama.init()  # needed for windows

        parser = argparse.ArgumentParser()
        parser.add_argument('--debug', '-d', action='store_true', help='show debug information')
        parser.add_argument('--start-date', type=str, default=None,
                            help='first date on which you want to book the first slot (format should be DD/MM/YYYY)')
        parser.add_argument('--motive-id', type=int, help='Optional consultation ID. If not provided, prompt for choices.')
        parser.add_argument('doctor_id', help='Doctolib ID of the doctor (as it appears in their page URL, usually firstname-lastname)')
        parser.add_argument('username', help='Doctolib username')
        parser.add_argument('password', nargs='?', help='Doctolib password')
        args = parser.parse_args(cli_args if cli_args else sys.argv[1:])

        if args.debug:
            responses_dirname = tempfile.mkdtemp(prefix='woob_session_')
            self.setup_loggers(logging.DEBUG)
        else:
            responses_dirname = None
            self.setup_loggers(logging.WARNING)

        if not args.password:
            args.password = getpass.getpass()

        docto = DoctolibFR(args.username, args.password, responses_dirname=responses_dirname)
        docto.load_state(self.load_state())

        try:
            if not docto.do_login():
                return 1

            patients = docto.get_patients()
            if len(patients) == 0:
                print("It seems that you don't have any Patient registered in your Doctolib account. Please fill your Patient data on Doctolib Website.")
                return 1
            if len(patients) > 1:
                print('Available patients are:')
                for i, patient in enumerate(patients):
                    print('* [%s] %s %s' %
                          (i, patient['first_name'], patient['last_name']))
                while True:
                    print('For which patient do you want to book a slot?',
                          end=' ', flush=True)
                    try:
                        docto.patient = patients[int(sys.stdin.readline().strip())]
                    except (ValueError, IndexError):
                        continue
                    else:
                        break
            else:
                docto.patient = patients[0]

            if args.start_date:
                try:
                    start_date = datetime.datetime.strptime(args.start_date, '%d/%m/%Y').date()
                except ValueError as e:
                    print('Invalid value for --start-date: %s' % e)
                    return 1
            else:
                start_date = datetime.date.today()

            log('Starting to look for available slots for %s %s from %s...',
                docto.patient['first_name'], docto.patient['last_name'], start_date)

            while not docto.has_availability(args, start_date):
                sleep(5)
            while True:
                playsound('ding.mp3')
                sleep(5)
        finally:
            self.save_state(docto.dump_state())


if __name__ == '__main__':
    Application().main()
