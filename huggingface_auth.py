import os
import time
from datetime import datetime, timedelta
from getpass import getpass

import requests
from huggingface_hub import HfApi
from termcolor import colored

from hivemind.proto.auth_pb2 import AccessToken
from hivemind.utils.auth import TokenAuthorizerBase
from hivemind.utils.crypto import RSAPublicKey
from hivemind.utils.logging import get_logger


logger = get_logger("root." + __name__)


class NonRetriableError(Exception):
    pass


def call_with_retries(func, n_retries=10, initial_delay=1.0):
    for i in range(n_retries):
        try:
            return func()
        except NonRetriableError:
            raise
        except Exception as e:
            if i == n_retries - 1:
                raise

            delay = initial_delay * (2 ** i)
            logger.warning(f'Failed to call `{func.__name__}` with exception: {e}. Retrying in {delay:.1f} sec')
            time.sleep(delay)


class InvalidCredentialsError(NonRetriableError):
    pass


class NotInAllowlistError(NonRetriableError):
    pass


class HuggingFaceAuthorizer(TokenAuthorizerBase):
    _AUTH_SERVER_URL = 'https://collaborative-training-auth.huggingface.co'

    def __init__(self, organization_name: str, model_name: str, hf_user_access_token: str):
        super().__init__()

        self.organization_name = organization_name
        self.model_name = model_name
        self.hf_user_access_token = hf_user_access_token

        self._authority_public_key = None
        self.coordinator_ip = None
        self.coordinator_port = None

        self._hf_api = HfApi()

    async def get_token(self) -> AccessToken:
        """
        Hivemind calls this method to refresh the token when necessary.
        """

        self.join_experiment()
        return self._local_access_token

    def join_experiment(self) -> None:
        call_with_retries(self._join_experiment)

    def _join_experiment(self) -> None:
        try:
            url = f'{self._AUTH_SERVER_URL}/api/experiments/join'
            headers = {'Authorization': f'Bearer {self.hf_user_access_token}'}
            response = requests.put(
                url,
                headers=headers,
                params={
                    'organization_name': self.organization_name,
                    'model_name': self.model_name,
                },
                json={
                    'experiment_join_input': {
                        'peer_public_key': self.local_public_key.to_bytes().decode(),
                    },
                },
                verify=False,  # FIXME: Update the expired API certificate
            )

            response.raise_for_status()
            response = response.json()

            self._authority_public_key = RSAPublicKey.from_bytes(response['auth_server_public_key'].encode())
            self.coordinator_ip = response['coordinator_ip']
            self.coordinator_port = response['coordinator_port']

            token_dict = response['hivemind_access']
            access_token = AccessToken()
            access_token.username = token_dict['username']
            access_token.public_key = token_dict['peer_public_key'].encode()
            access_token.expiration_time = str(datetime.fromisoformat(token_dict['expiration_time']))
            access_token.signature = token_dict['signature'].encode()
            self._local_access_token = access_token
            logger.info(f'Access for user {access_token.username} '
                        f'has been granted until {access_token.expiration_time} UTC')
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:  # Unauthorized
                raise NotInAllowlistError()
            raise

    def is_token_valid(self, access_token: AccessToken) -> bool:
        data = self._token_to_bytes(access_token)
        if not self._authority_public_key.verify(data, access_token.signature):
            logger.exception('Access token has invalid signature')
            return False

        try:
            expiration_time = datetime.fromisoformat(access_token.expiration_time)
        except ValueError:
            logger.exception(
                f'datetime.fromisoformat() failed to parse expiration time: {access_token.expiration_time}')
            return False
        if expiration_time.tzinfo is not None:
            logger.exception(f'Expected to have no timezone for expiration time: {access_token.expiration_time}')
            return False
        if expiration_time < datetime.utcnow():
            logger.exception('Access token has expired')
            return False

        return True

    _MAX_LATENCY = timedelta(minutes=1)

    def does_token_need_refreshing(self, access_token: AccessToken) -> bool:
        expiration_time = datetime.fromisoformat(access_token.expiration_time)
        return expiration_time < datetime.utcnow() + self._MAX_LATENCY

    @staticmethod
    def _token_to_bytes(access_token: AccessToken) -> bytes:
        return f'{access_token.username} {access_token.public_key} {access_token.expiration_time}'.encode()


def authorize_with_huggingface() -> HuggingFaceAuthorizer:
    while True:
        organization_name = os.getenv('HF_ORGANIZATION_NAME')
        if organization_name is None:
            organization_name = input('HuggingFace organization name: ')

        model_name = os.getenv('HF_MODEL_NAME')
        if model_name is None:
            model_name = input('HuggingFace model name: ')

        hf_user_access_token = os.getenv('HF_USER_ACCESS_TOKEN')
        if hf_user_access_token is None:
            msg = [
                "Copy a token from your Hugging Face tokens page at ",
                colored("https://huggingface.co/settings/token", attrs=['bold']),
                "and paste it.\n💡",
                colored("Pro Tip:", attrs=['bold']),
                "If you don't already have one, you can create a dedicated user access token. Go to "
                "https://huggingface.co/settings/token and click on the `new token` button. ",
                "You just need to give a ",
                colored("'read'", attrs=['bold']),
                "role to this access token." ,
                "Don't forget to give an explicit name to this access token like",
                colored(f"'{organization_name}-{model_name}-collaborative-training'", attrs=['bold'])
                ]
            print(*msg)
            hf_user_access_token = getpass('HF user access token : ')

            authorizer = HuggingFaceAuthorizer(organization_name, model_name, hf_user_access_token)

        try:
            authorizer.join_experiment()

            username = authorizer._local_access_token.username
            print(f"🚀 You will contribute to the collaborative training under the username {username}.")
            return authorizer
        except InvalidCredentialsError:
            print('Invalid user access token, please try again')
        except NotInAllowlistError:
            print(
                '😥 Authentication has failed. '
                'This error may be due to the fact:\n',
                "   1. your user access token is not valid. You can try to delete the previous token and"
                " recreate one. Be careful, organization tokens can't be used to join a collaborative "
                "training.\n"
                f"   2. you have not yet joined the {organization_name} organization. You can request to"
                " join this organization by clicking on the 'request to join this org' button at "
                f"https://huggingface.co/{organization_name}.\n",
                f"   3. the {organization_name} organization doesn't exist at https://huggingface.co/{organization_name}.\n",
                f"   4. no {organization_name}'s admin has created a collaborative training for the {organization_name}"
                f"organization and the {model_name} model.",
                )
    

