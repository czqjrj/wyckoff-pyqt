import os, sys
if getattr(sys, 'frozen', False):
    _base = sys._MEIPASS
    _cert = os.path.join(_base, 'certifi', 'cacert.pem')
    if os.path.isfile(_cert):
        os.environ['SSL_CERT_FILE'] = _cert
        os.environ['REQUESTS_CA_BUNDLE'] = _cert
