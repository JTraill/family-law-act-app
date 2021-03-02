import logging
import json
import uuid
import base64
from django.utils import timezone
from django.conf import settings
from django.http import JsonResponse, HttpResponse
from rest_framework import permissions, generics
from api.efiling import EFilingPackaging, EFilingParsing, EFilingSubmission
from api.models import PreparedPdf, EFilingSubmission as EFilingSubmissionModel
from api.utils import get_application_for_user
from core.pdf import image_to_pdf
from core.utils.json_message_response import JsonMessageResponse

logger = logging.getLogger(__name__)


class EFilingSubmitView(generics.GenericAPIView):
    permission_classes = (permissions.IsAuthenticated,)

    def __init__(self):
        self.efiling_parsing = EFilingParsing()
        self.efiling_submission = EFilingSubmission(EFilingPackaging())
        self.file_byte_size_limit = 1024 * 1024 * 10  # 10 MBytes
        self.allowed_extensions = ["pdf", "jpg", "jpeg", "gif", "png"]

    def _file_size_too_large(self, size):
        return size > self.file_byte_size_limit

    def _invalid_file_extension(self, file):
        extension = file.name.split(".")[-1]
        return extension.lower() not in self.allowed_extensions

    def _get_validation_errors(self, request_files):
        for file in request_files:
            if file.size == 0:
                return JsonMessageResponse("One of the files was empty.", status=400)
            if self._file_size_too_large(file.size):
                return JsonMessageResponse(
                    "Filesize limit exceeded: 10 MB.", status=400
                )
            if self._invalid_file_extension(file):
                return JsonMessageResponse("Wrong file format.", status=400)
        return None

    """ This inserts our generated file, iterates over files and converts to PDF if necessary.
        Also converts MemoryUploadedFiles into a multi-form payload to be sent out as files. """

    def _convert_incoming_files(self, incoming_files, po_pdf_content, document_types):
        outgoing_files = [
            ("files", ("fpo_generated.pdf", po_pdf_content, "application/pdf"))
        ]
        document_types.insert(0, "POR")

        # Convert files, if they aren't PDF.
        for incoming_file in incoming_files:
            outgoing_files.append(
                (
                    "files",
                    (
                        incoming_file.name
                        if incoming_file.name.endswith(".pdf")
                        else f"{incoming_file.name.rsplit('.', 1)[0] + '.pdf'}",
                        incoming_file.read()
                        if incoming_file.name.endswith(".pdf")
                        else image_to_pdf(
                            {
                                "images": [
                                    {
                                        "base64": base64.b64encode(
                                            incoming_file.read()
                                        ).decode("utf-8"),
                                        "type": incoming_file.name.lower().split(".")[
                                            -1
                                        ],
                                    }
                                ]
                            }
                        ),
                        "application/pdf",
                    ),
                )
            )
        return document_types, outgoing_files

    def _get_protection_order_content(self, prepared_pdf_id, application_id):
        prepared_pdf = PreparedPdf.objects.get(id=prepared_pdf_id)
        po_pdf_content = settings.ENCRYPTOR.decrypt(
            prepared_pdf.key_id, prepared_pdf.data
        )
        po_json = json.loads(
            settings.ENCRYPTOR.decrypt(
                prepared_pdf.key_id, prepared_pdf.json_data
            ).decode("utf-8")
        )
        po_json.update({"applicationId": application_id})
        return (po_pdf_content, po_json)

    def put(self, request, application_id):
        body = request.data
        application = get_application_for_user(application_id, request.user.id)
        efiling_submission = EFilingSubmissionModel.objects.filter(
            submission_id=application.last_submission_id
        ).first()
        if efiling_submission:
            efiling_submission.package_number = body.get("packageNumber")
            efiling_submission.package_url = body.get("packageUrl")
            efiling_submission.last_updated = timezone.now()
            efiling_submission.save()
            return HttpResponse(status=204)
        else:
            return HttpResponse(status=404)

    def post(self, request, application_id):
        document_types = request.POST.getlist("documentTypes")
        request_files = request.FILES.getlist("files")

        # Validations.
        validations_errors = self._get_validation_errors(request_files)
        if validations_errors:
            return validations_errors

        application = get_application_for_user(application_id, request.user.id)
        if application.prepared_pdf_id is None:
            return JsonMessageResponse("PO PDF is not generated.", status=400)

        # Data conversion.
        po_pdf_content, po_json = self._get_protection_order_content(
            application.prepared_pdf_id, application_id
        )
        document_types, outgoing_files = self._convert_incoming_files(
            request_files, po_pdf_content, document_types
        )
        data = self.efiling_parsing.convert_data_for_efiling(
            request, application, po_json, outgoing_files, document_types
        )

        # EFiling upload document.
        transaction_id = str(uuid.uuid4())
        efiling_submission = EFilingSubmissionModel(
            transaction_id=transaction_id,
            application_id=application.id,
        )
        efiling_submission.save()
        upload_result = self.efiling_submission.upload_documents(
            request.user.universal_id, transaction_id, outgoing_files
        )

        if upload_result is None or "submissionId" not in upload_result:
            message = (
                upload_result["message"]
                if upload_result and "message" in upload_result
                else "Document Upload Failed."
            )
            return JsonMessageResponse(message, status=500)

        # EFiling package submission.
        submission_id = upload_result["submissionId"]
        redirect_url, message = self.efiling_submission.generate_efiling_url(
            request.user.universal_id, transaction_id, submission_id, data
        )

        if redirect_url is not None:
            application.last_submission_id = submission_id
            application.save()

            efiling_submission.submission_id = submission_id
            efiling_submission.last_updated = timezone.now()
            efiling_submission.save()
            return JsonResponse({"redirectUrl": redirect_url, "message": message})

        return JsonMessageResponse(message, status=500)
