# Copyright 2021 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
import bravado
import pytest
import requests

from common import Device, DevAuthorizer, device_auth_req, \
    explode_jwt, \
    clean_migrated_db, clean_db, mongo, cli, \
    management_api, internal_api, device_api

import orchestrator


def request_token(device, dev_auth, url):
    # device is accepted, we should get a token now
    rsp = device_auth_req(url, dev_auth, device)
    assert rsp.status_code == 200

    dev_auth.parse_rsp_payload(device, rsp.text)
    return device.token


@pytest.yield_fixture(scope='function')
def accepted_device(device_api, management_api, clean_migrated_db):
    """Fixture that sets up an accepted device. Yields a tuple:
       (device ID, instance of Device, instance of DevAuthorizer)"""
    d = Device()
    da = DevAuthorizer()
    url = device_api.auth_requests_url

    try:
        with orchestrator.run_fake_for_device_id(1) as server:
            # poke devauth so that device appears
            rsp = device_auth_req(url, da, d)
            assert rsp.status_code == 401

            # try to find our devices in all devices listing
            dev = management_api.find_device_by_identity(d.identity)

            print('found matching device with ID', dev.id)
            devid = dev.id
            # extract authentication data set ID
            aid = dev.auth_sets[0].id

        with orchestrator.run_fake_for_device_id(devid) as server:
            management_api.accept_device(devid, aid)
    except bravado.exception.HTTPError as e:
        assert e.response.status_code == 204

    yield (devid, d, da)


@pytest.yield_fixture(scope='function')
def device_token(accepted_device, device_api):
    devid, d, da = accepted_device

    try:
        with orchestrator.run_fake_for_device_id(devid) as server:
            token = request_token(d, da, device_api.auth_requests_url)
    except bravado.exception.HTTPError as e:
        assert e.response.status_code == 204

    print("device token:", token)
    assert token
    yield token


@pytest.yield_fixture(scope='session')
def token_verify_url(internal_api):
    verify_url = internal_api.make_api_url("/tokens/verify")
    print("verify URL:", verify_url)
    yield verify_url


class TestToken:
    def test_token_claims(self, accepted_device, management_api, device_api):
        devid, d, da = accepted_device

        try:
            with orchestrator.run_fake_for_device_id(devid) as server:
                token = request_token(d, da, device_api.auth_requests_url)
        except bravado.exception.HTTPError as e:
            assert e.response.status_code == 204

        assert len(token) > 0
        print("device token:", d.token)

        thdr, tclaims, tsign = explode_jwt(d.token)
        assert 'typ' in thdr and thdr['typ'] == 'JWT'

        assert 'jti' in tclaims
        assert 'exp' in tclaims
        assert 'sub' in tclaims and tclaims['sub'] == devid
        assert 'iss' in tclaims and tclaims['iss'] == 'Mender'
        assert 'mender.device' in tclaims and tclaims['mender.device'] == True

    def test_token_verify_ok(self, device_token, token_verify_url):

        # verify token; the token is to be placed in the Authorization header
        # and it looks like bravado cannot handle a POST request with no data
        # in body, hence we fall back to sending request directly
        auth_hdr = 'Bearer {}'.format(device_token)
        # successful verification
        rsp = requests.post(token_verify_url, data='',
                            headers={'Authorization': auth_hdr})
        assert rsp.status_code == 200

    def test_token_verify_none(self, token_verify_url):
        # no auth header should raise an error
        rsp = requests.post(token_verify_url, data='')
        assert rsp.status_code == 401

    def test_token_verify_bad(self, token_verify_url):
        # use a bogus token that is not a valid JWT
        rsp = requests.post(token_verify_url, data='',
                            headers={'Authorization': 'bogus'})
        assert rsp.status_code == 401

    def test_token_verify_corrupted(self, device_token, token_verify_url):
        auth_hdr = 'Bearer {}'.format(device_token)

        rsp = requests.post(token_verify_url, data='',
                            headers={'Authorization': auth_hdr + "==foo"})
        assert rsp.status_code == 401

    def test_token_delete(self, device_token, token_verify_url, management_api):
        _, tclaims, _ = explode_jwt(device_token)

        # bravado cannot handle DELETE requests either
        #   self.client.tokens.delete_tokens_id(id=tclaims['jti'])
        # use requests instead
        rsp = requests.delete(management_api.make_api_url('/tokens/{}'.format(tclaims['jti'])))
        assert rsp.status_code == 204

        auth_hdr = 'Bearer {}'.format(device_token)
        # unsuccessful verification
        rsp = requests.post(token_verify_url, data='',
                            headers={'Authorization': auth_hdr})
        assert rsp.status_code == 401
