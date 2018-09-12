import re
from datetime import timedelta
from dateutil.parser import parse as parse_date

import requests
from invenio_db import db
from invenio_pidstore.models import PersistentIdentifier

from scoap3.modules.compliance.models import Compliance
from scoap3.utils.pdf import extract_text_from_pdf


def __get_first_doi(obj):
    return obj.data['dois'][0]['value']


def __extract_text_as_extra_data(obj):
    # fixme extraction shouldn't happen in article_upload?
    # do extraction only if not done earlier
    if 'extracted_pdf' in obj.extra_data:
        return

    path = None
    for file in obj.extra_data['files']:
        if file['filetype'] in ('pdf', 'pdf/a'):
            path = file['url']
    obj.extra_data['extracted_pdf'] = extract_text_from_pdf(path)


def __find_regexp_in_pdf(obj, pattern):
    __extract_text_as_extra_data(obj)

    match = re.search(pattern, obj.extra_data['extracted_pdf'], re.IGNORECASE)

    if not match:
        return False, None, None

    details = 'Found as "%s"' % match.group(0)
    return True, details, None


def _files(obj):
    """ check if it has the necessary files: .xml, .pdf, .pdfa """

    file_types = [file['filetype'] for file in obj.data['_files']]

    ok = True
    details = ''

    if 'xml' not in file_types:
        ok = False
        details += 'No xml file. '

    if 'pdf' not in file_types and 'pdf/a' not in file_types:
        ok = False
        details += 'No pdf file. '

    details += 'Available files: %s' % ', '.join(file_types)

    return ok, details, file_types


def _received_in_time(obj):
    """ check if publication is not older than 24h """
    api_url = 'https://api.crossref.org/works/%s'

    api_message = requests.get(api_url % __get_first_doi(obj)).json()['message']

    # FIXME published-online only contains date of publication. Should we use that?
    # api_time = api_message.get('published-online', api_message['created'])

    api_time = parse_date(api_message['created']['date-time'], ignoretz=True)
    received_time = parse_date(obj.data['acquisition_source']['date'])
    delta = received_time - api_time

    ok = delta <= timedelta(hours=24)
    details_message = 'Arrived %d hours later then creation date on crossref.org.' % (delta.total_seconds() / 3600)
    debug = 'Time from crossref: %s, Received time: %s' % (api_time, received_time)

    return ok, details_message, debug


def _founded_by(obj):
    """check if publication has "Founded by SCOAP3" marking *in pdf(a) file* """

    pattern = '.{0,10}scoap(3){0,1}.{0,10}'
    return __find_regexp_in_pdf(obj, pattern)


def _author_rights(obj):
    # todo has author rights: ?
    return True, None, None


def _cc_licence(obj):
    # has 'cc-by', 'cc by' or 'creative commons attribution'
    pattern = '(cc-{0,1}by)|(creative commons attribution)'
    return __find_regexp_in_pdf(obj, pattern)


COMPLIANCE_TASKS = [
    ('files', _files),
    ('in_time', _received_in_time),
    ('founded_by', _founded_by),
    ('author_rights', _author_rights),
    ('cc_licence', _cc_licence),
]


def check_compliance(obj, eng):
    checks = {}
    all_ok = True
    for name, function in COMPLIANCE_TASKS:
        ok, details, debug = function(obj)
        all_ok = all_ok and ok
        checks[name] = {
            'check': ok,
            'details': details,
            'debug': debug
        }

    c = Compliance()
    results = {'checks': checks}
    c.results = results
    pid = PersistentIdentifier.get('recid', obj.extra_data['recid'])
    c.id_record = pid.object_uuid

    db.session.add(c)
    db.session.commit()

    # todo send notif