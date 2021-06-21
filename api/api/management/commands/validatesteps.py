import json
import datetime
import pytz
import subprocess
import jsonschema

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import migrations
from django.utils import timezone
from jsonschema import validate
from api.models import Application
from api.migrations.helpers import Migration_1_0_to_1_1


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("file_path")

    def handle(self, *args, **options):
        file_path = options["file_path"]
        write_to_file = False  # for testing.
        print(f"Ensure generate_schema was recently ran, so your schema is up to date.")
        print(f"Reading schema from {file_path}")
        f = open(
            file_path,
        )
        schema = json.load(f)
        f.close()
        for application in Application.objects.filter(
            last_updated__gte=datetime.datetime(2021, 6, 14).astimezone()
        ):
            steps_json = json.loads(
                settings.ENCRYPTOR.decrypt(
                    application.key_id, application.steps
                ).decode("utf-8")
            )
            # print('Before: ')
            # print(json.dumps(steps_json, indent=4).replace('\r\n',''))
            if write_to_file:
                f = open(f"before-{application.id}.txt", "w")
                json.dump(steps_json, skipkeys=False, fp=f, sort_keys=True, indent=4)
                f.close()

            print(f"Validating steps schema for application Id: {application.id}")
            steps_json = Migration_1_0_to_1_1().migrate(steps_json)
            # print('After: ')
            # print(json.dumps(steps_json, indent=4).replace('\r\n',''))
            if write_to_file:
                f = open(f"after-{application.id}.txt", "w")
                json.dump(steps_json, fp=f, skipkeys=False, sort_keys=True, indent=4)
                f.close()

            validator = jsonschema.Draft7Validator(schema)
            errors = validator.iter_errors({"steps": steps_json})

            for error in errors:
                print(error)
                print("------")
