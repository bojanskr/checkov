from __future__ import annotations

import json
import logging
import os
from typing import Set, Any, Generator, Pattern, Optional, Dict, Tuple, cast, List

import yaml  # type: ignore
from detect_secrets.constants import VerifiedResult
from detect_secrets.core.potential_secret import PotentialSecret
from detect_secrets.plugins.base import RegexBasedDetector
import re

from checkov.common.bridgecrew.platform_integration import bc_integration
from detect_secrets.util.code_snippet import CodeSnippet
from detect_secrets.util.inject import call_function_with_arguments


DETECTORS_BY_CUSTOMER_CACHE: dict[str, list[dict[str, Any]]] = {}


def get_customer_cache() -> dict[str, list[dict[str, Any]]]:
    return DETECTORS_BY_CUSTOMER_CACHE


def get_detectors_from_cache(customer_name: str | None) -> list[dict[str, Any]]:
    if customer_name:
        cache = get_customer_cache()
        return cache.get(customer_name, [])
    return []


def get_detectors_from_local_file() -> list[dict[str, Any]]:
    current_dir = os.path.dirname(os.path.realpath(__file__))
    with open(f'{current_dir}/detectors.json') as f:
        return cast("list[dict[str, Any]]", json.load(f))


def load_detectors() -> list[dict[str, Any]]:
    customer_name = os.getenv('CUSTOMER_NAME')
    detectors = get_detectors_from_cache(customer_name)
    if not detectors:
        try:
            customer_run_config_response = bc_integration.customer_run_config_response
            policies_list:  List[dict[str, Any]] | dict[str, Any] = customer_run_config_response['secretsPolicies'] if \
                customer_run_config_response['secretsPolicies'] else []
        except Exception as e:
            return []

        if policies_list:
            if isinstance(policies_list, dict):
                policies_list = [policies_list]
            detectors = modify_secrets_policy_to_detectors(policies_list)

        if customer_name:
            DETECTORS_BY_CUSTOMER_CACHE[customer_name] = detectors

    logging.info(f'Successfully loaded {len(detectors)} detectors from s3')
    return detectors


def modify_secrets_policy_to_detectors(policies_list: List[dict[str, Any]]) -> List[dict[str, Any]]:
    secrets_list = transforms_policies_to_detectors_list(policies_list)
    logging.info(f'(modify_secrets_policy_to_detectors) secrets_list = {secrets_list}')
    return secrets_list


def transforms_policies_to_detectors_list(custom_secrets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    custom_detectors: List[Dict[str, Any]] = []
    for secret_policy in custom_secrets:
        not_parsed = True
        code = secret_policy['code']
        if code:
            code_dict = yaml.safe_load(secret_policy['code'])
            if 'definition' in code_dict:
                if 'value' in code_dict['definition']:
                    not_parsed = False
                    for regex in code_dict['definition']['value']:
                        check_id = secret_policy['checkovCheckId'] if secret_policy['checkovCheckId'] else \
                        secret_policy['incidentId']
                        custom_detectors.append({'Name': secret_policy['title'],
                                                 'Check_ID': check_id,
                                                 'Regex': regex})
    return custom_detectors


class CustomRegexDetector(RegexBasedDetector):
    secret_type = "Regex Detector"

    denylist: Set[Pattern[str]] = set()

    def __init__(self) -> None:
        self.regex_to_metadata: dict[str, dict[str, Any]] = dict()
        self.denylist = set()
        detectors = load_detectors()

        for detector in detectors:
            self.denylist.add(re.compile(r'{}'.format(detector["Regex"])))
            self.regex_to_metadata[detector["Regex"]] = detector

    def analyze_line(
            self,
            filename: str,
            line: str,
            line_number: int = 0,
            context: Optional[CodeSnippet] = None,
            raw_context: Optional[CodeSnippet] = None,
            **kwargs: Any
    ) -> Set[PotentialSecret]:
        """This examines a line and finds all possible secret values in it."""
        output: Set[PotentialSecret] = set()
        for match, regex in self.analyze_string(line, **kwargs):
            try:
                verified_result = call_function_with_arguments(self.verify, secret=match, context=context)
                is_verified = True if verified_result == VerifiedResult.VERIFIED_TRUE else False
            except Exception:
                is_verified = False

            ps = PotentialSecret(type=self.regex_to_metadata[regex.pattern]["Name"], filename=filename, secret=match,
                                 line_number=line_number, is_verified=is_verified)
            ps.check_id = self.regex_to_metadata[regex.pattern]["Check_ID"]  # type:ignore[attr-defined]
            output.add(ps)

        return output

    def analyze_string(self, string: str, **kwargs: Optional[Dict[str, Any]]) -> Generator[
        Tuple[str, Pattern[str]], None, None]:  # type:ignore[override]
        for regex in self.denylist:
            for match in regex.findall(string):
                if isinstance(match, tuple):
                    for submatch in filter(bool, match):
                        # It might make sense to paste break after yielding
                        yield submatch, regex
                else:
                    yield match, regex
