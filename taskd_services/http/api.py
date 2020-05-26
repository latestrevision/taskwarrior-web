from datetime import date, datetime
import json
import os
import subprocess
import tempfile
import uuid

from flask import Flask, request, redirect, send_file
from flask_restful import Resource, Api
from flask_sqlalchemy import SQLAlchemy


TASKD_BINARY = os.environ.get('TASKD_BINARY', '/usr/bin/taskd')
TASKD_DATA = os.environ['TASKDDATA']
CA_CERT = os.environ['CA_CERT']
CA_KEY = os.environ['CA_KEY']
CA_SIGNING_TEMPLATE = os.environ['CA_SIGNING_TEMPLATE']
CERT_DB_PATH = os.environ.get(
    'CERT_DB_PATH', 
    os.path.join(TASKD_DATA, 'certificates.sqlite3')
)


class TaskdJsonEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.strftime('%Y-%m-%dT%H:%M:%SZ')
        if isinstance(obj, date):
            return obj.strftime('%Y-%m-%d')

        return super().default(self, obj)


app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + CERT_DB_PATH
app.config['RESTFUL_JSON'] = {'cls': TaskdJsonEncoder}
api = Api(app)

db = SQLAlchemy(app)


class Credential(db.Model):
    user_key = db.Column(db.String(36), nullable=False, primary_key=True)
    org_name = db.Column(db.String(255), nullable=False)
    user_name = db.Column(db.String(255), nullable=False)
    created = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    deleted = db.Column(db.DateTime, nullable=True)

    def as_dict(self):
        return {
            'credentials': f'{self.org_name}/{self.user_name}/{self.user_key}',
        }


class Certificate(db.Model):
    id = db.Column(
        db.String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4())
    )
    user_key = db.Column(db.String(36), nullable=False)
    certificate = db.Column(db.Text, nullable=False)
    created = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    revoked = db.Column(db.DateTime, nullable=True)

    def as_dict(self):
        return {
            'id': self.id,
            'certificate': self.certificate,
            'created': self.created,
            'revoked': self.revoked,
        }


class TaskdError(Exception):
    pass


class TaskdAccount(Resource):
    def put(self, org_name, user_name):
        env = os.environ.copy()
        env['TASKDDATA'] = TASKD_DATA

        command = [
            TASKD_BINARY,
            'add',
            'user',
            org_name,
            user_name
        ]
        key_proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        key_proc_output = (
            key_proc.communicate()[0].decode('utf-8').split('\n')
        )
        taskd_user_key = (
            key_proc_output[0].split(':')[1].strip()
        )
        result = key_proc.wait()
        if result != 0:
            raise TaskdError()

        cred = Credential(
            org_name=org_name,
            user_name=user_name,
            user_key=taskd_user_key,
        )
        db.session.add(cred)
        db.session.commit()

        return redirect(
            api.url_for(
                TaskdAccount,
                org_name=org_name,
                user_name=user_name,
            )
        )

    def get(self, org_name, user_name):
        cred = Credential.query.filter_by(
            user_name=user_name,
            org_name=org_name,
        ).first_or_404()

        return cred.as_dict()

    def delete(self, org_name, user_name):
        cred = Credential.query.filter_by(
            user_name=user_name,
            org_name=org_name,
        ).first_or_404()

        env = os.environ.copy()
        env['TASKDDATA'] = TASKD_DATA
        command = [
            TASKD_BINARY,
            'remove',
            'user',
            org_name,
            user_name
        ]
        delete_proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        result = delete_proc.wait()
        if result != 0:
            raise TaskdError()

        cred.deleted = datetime.utcnow()
        db.session.commit()

        return None


class TaskdCertificates(Resource):
    def post(self, org_name, user_name):
        cred = Credential.query.filter_by(
            user_name=user_name,
            org_name=org_name,
        ).first_or_404()

        with tempfile.NamedTemporaryFile('wb+') as outf:
            outf.write(request.data)
            outf.flush()

            cert_proc = subprocess.Popen(
                [
                    'certtool',
                    '--generate-certificate',
                    '--load-request',
                    outf.name,
                    '--load-ca-certificate',
                    CA_CERT,
                    '--load-ca-privkey',
                    CA_KEY,
                    '--template',
                    CA_SIGNING_TEMPLATE
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            cert = cert_proc.communicate()[0].decode('utf-8')

        cert_record = Certificate(
            user_key=cred.user_key,
            certificate=cert,
        )
        db.session.add(cert_record)
        db.session.commit()

        return redirect(
            api.url_for(
                TaskdCertificateDetails,
                org_name=org_name,
                user_name=user_name,
                cert_id=cert_record.id,
            )
        )

    def get(self, org_name, user_name):
        cred = Credential.query.filter_by(
            user_name=user_name,
            org_name=org_name,
        ).first_or_404()

        return [
            record.as_dict()
            for record in Certificate.query.filter_by(
                user_key=cred.user_key,
            )
        ]


class TaskdCertificateDetails(Resource):
    def get(self, org_name, user_name, cert_id):
        cred = Credential.query.filter_by(
            user_name=user_name,
            org_name=org_name,
        ).first_or_404()
        cert_record = Certificate.query.filter_by(
            user_key=cred.user_key,
            id=cert_id,
        ).first_or_404()

        return cert_record.as_dict()

    def delete(self, org_name, user_name, cert_id):
        cred = Credential.query.filter_by(
            user_name=user_name,
            org_name=org_name,
        ).first_or_404()
        cert_record = Certificate.query.filter_by(
            user_key=cred.user_key,
            id=cert_id,
        ).first_or_404()

        cert_record.revoked = datetime.utcnow()

        db.session.commit()

        return None


class TaskdData(Resource):
    def get_data_path(self, cred) -> str:
        return os.path.join(
            TASKD_DATA,
            'orgs',
            cred.org_name,
            'users',
            cred.user_key,
            'tx.data',
        )

    def get(self, org_name, user_name):
        cred = Credential.query.filter_by(
            user_name=user_name,
            org_name=org_name,
        ).first_or_404()

        return send_file(
            self.get_data_path(cred),
            mimetype='application/octet-stream',
            as_attachment=True,
        )

    def delete(self, org_name, user_name):
        cred = Credential.query.filter_by(
            user_name=user_name,
            org_name=org_name,
        ).first_or_404()

        os.unlink(self.get_data_path(cred))

        return None


api.add_resource(TaskdAccount, '/<org_name>/<user_name>')
api.add_resource(TaskdCertificates, '/<org_name>/<user_name>/certificates/')
api.add_resource(
    TaskdCertificateDetails,
    '/<org_name>/<user_name>/certificates/<cert_id>'
)
api.add_resource(TaskdData, '/<org_name>/<user_name>/data/')


if __name__ == '__main__':
    db.create_all()
    app.run(port=80, host='0.0.0.0')
