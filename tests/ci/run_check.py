#!/usr/bin/env python3
import sys
import logging
import re
from typing import Tuple

from github import Github

from commit_status_helper import get_commit, post_labels, remove_labels
from env_helper import GITHUB_RUN_URL, GITHUB_REPOSITORY, GITHUB_SERVER_URL
from get_robot_token import get_best_robot_token
from pr_info import PRInfo
from workflow_approve_rerun_lambda.app import TRUSTED_CONTRIBUTORS

NAME = "Run Check (actions)"

TRUSTED_ORG_IDS = {
    7409213,  # yandex
    28471076,  # altinity
    54801242,  # clickhouse
}

OK_SKIP_LABELS = {"release", "pr-backport", "pr-cherrypick"}
CAN_BE_TESTED_LABEL = "can be tested"
DO_NOT_TEST_LABEL = "do not test"
FORCE_TESTS_LABEL = "force tests"
SUBMODULE_CHANGED_LABEL = "submodule changed"


MAP_CATEGORY_TO_LABEL = {
    "New Feature": "pr-feature",
    "Bug Fix": "pr-bugfix",
    "Bug Fix (user-visible misbehaviour in official "
    "stable or prestable release)": "pr-bugfix",
    "Improvement": "pr-improvement",
    "Performance Improvement": "pr-performance",
    "Backward Incompatible Change": "pr-backward-incompatible",
    "Build/Testing/Packaging Improvement": "pr-build",
    "Build Improvement": "pr-build",
    "Build/Testing Improvement": "pr-build",
    "Build": "pr-build",
    "Packaging Improvement": "pr-build",
    "Not for changelog (changelog entry is not required)": "pr-not-for-changelog",
    "Not for changelog": "pr-not-for-changelog",
    "Documentation (changelog entry is not required)": "pr-documentation",
    "Documentation": "pr-documentation",
    # 'Other': doesn't match anything
}


def pr_is_by_trusted_user(pr_user_login, pr_user_orgs):
    if pr_user_login.lower() in TRUSTED_CONTRIBUTORS:
        logging.info("User '%s' is trusted", pr_user_login)
        return True

    logging.info("User '%s' is not trusted", pr_user_login)

    for org_id in pr_user_orgs:
        if org_id in TRUSTED_ORG_IDS:
            logging.info(
                "Org '%s' is trusted; will mark user %s as trusted",
                org_id,
                pr_user_login,
            )
            return True
        logging.info("Org '%s' is not trusted", org_id)

    return False


# Returns whether we should look into individual checks for this PR. If not, it
# can be skipped entirely.
# Returns can_run, description, labels_state
def should_run_checks_for_pr(pr_info: PRInfo) -> Tuple[bool, str, str]:
    # Consider the labels and whether the user is trusted.
    print("Got labels", pr_info.labels)
    if FORCE_TESTS_LABEL in pr_info.labels:
        return True, f"Labeled '{FORCE_TESTS_LABEL}'", "pending"

    if DO_NOT_TEST_LABEL in pr_info.labels:
        return False, f"Labeled '{DO_NOT_TEST_LABEL}'", "success"

    if CAN_BE_TESTED_LABEL not in pr_info.labels and not pr_is_by_trusted_user(
        pr_info.user_login, pr_info.user_orgs
    ):
        return False, "Needs 'can be tested' label", "failure"

    if OK_SKIP_LABELS.intersection(pr_info.labels):
        return (
            False,
            "Don't try new checks for release/backports/cherry-picks",
            "success",
        )

    return True, "No special conditions apply", "pending"


def check_pr_description(pr_info):
    description = pr_info.body

    lines = list(
        map(lambda x: x.strip(), description.split("\n") if description else [])
    )
    lines = [re.sub(r"\s+", " ", line) for line in lines]

    category = ""
    entry = ""

    i = 0
    while i < len(lines):
        if re.match(r"(?i)^[#>*_ ]*change\s*log\s*category", lines[i]):
            i += 1
            if i >= len(lines):
                break
            # Can have one empty line between header and the category
            # itself. Filter it out.
            if not lines[i]:
                i += 1
                if i >= len(lines):
                    break
            category = re.sub(r"^[-*\s]*", "", lines[i])
            i += 1

            # Should not have more than one category. Require empty line
            # after the first found category.
            if i >= len(lines):
                break
            if lines[i]:
                second_category = re.sub(r"^[-*\s]*", "", lines[i])
                result_status = (
                    "More than one changelog category specified: '"
                    + category
                    + "', '"
                    + second_category
                    + "'"
                )
                return result_status[:140], category

        elif re.match(
            r"(?i)^[#>*_ ]*(short\s*description|change\s*log\s*entry)", lines[i]
        ):
            i += 1
            # Can have one empty line between header and the entry itself.
            # Filter it out.
            if i < len(lines) and not lines[i]:
                i += 1
            # All following lines until empty one are the changelog entry.
            entry_lines = []
            while i < len(lines) and lines[i]:
                entry_lines.append(lines[i])
                i += 1
            entry = " ".join(entry_lines)
            # Don't accept changelog entries like '...'.
            entry = re.sub(r"[#>*_.\- ]", "", entry)
        else:
            i += 1

    if not category:
        return "Changelog category is empty", category

    # Filter out the PR categories that are not for changelog.
    if re.match(
        r"(?i)doc|((non|in|not|un)[-\s]*significant)|(not[ ]*for[ ]*changelog)",
        category,
    ):
        return "", category

    if not entry:
        return f"Changelog entry required for category '{category}'", category

    return "", category


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    pr_info = PRInfo(need_orgs=True, pr_event_from_api=True, need_changed_files=True)
    can_run, description, labels_state = should_run_checks_for_pr(pr_info)
    gh = Github(get_best_robot_token())
    commit = get_commit(gh, pr_info.sha)

    description_report, category = check_pr_description(pr_info)
    pr_labels_to_add = []
    pr_labels_to_remove = []
    if (
        category in MAP_CATEGORY_TO_LABEL
        and MAP_CATEGORY_TO_LABEL[category] not in pr_info.labels
    ):
        pr_labels_to_add.append(MAP_CATEGORY_TO_LABEL[category])

    for label in pr_info.labels:
        if (
            label in MAP_CATEGORY_TO_LABEL.values()
            and category in MAP_CATEGORY_TO_LABEL
            and label != MAP_CATEGORY_TO_LABEL[category]
        ):
            pr_labels_to_remove.append(label)

    if pr_info.has_changes_in_submodules():
        pr_labels_to_add.append(SUBMODULE_CHANGED_LABEL)
    elif SUBMODULE_CHANGED_LABEL in pr_info.labels:
        pr_labels_to_remove.append(SUBMODULE_CHANGED_LABEL)

    print(f"change labels: add {pr_labels_to_add}, remove {pr_labels_to_remove}")
    if pr_labels_to_add:
        post_labels(gh, pr_info, pr_labels_to_add)

    if pr_labels_to_remove:
        remove_labels(gh, pr_info, pr_labels_to_remove)

    if description_report:
        print(
            "::error ::Cannot run, PR description does not match the template: "
            f"{description_report}"
        )
        logging.info(
            "PR body doesn't match the template: (start)\n%s\n(end)\n" "Reason: %s",
            pr_info.body,
            description_report,
        )
        url = (
            f"{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/"
            "blob/master/.github/PULL_REQUEST_TEMPLATE.md?plain=1"
        )
        commit.create_status(
            context=NAME,
            description=description_report[:139],
            state="failure",
            target_url=url,
        )
        sys.exit(1)

    url = GITHUB_RUN_URL
    if not can_run:
        print("::notice ::Cannot run")
        commit.create_status(
            context=NAME, description=description, state=labels_state, target_url=url
        )
        sys.exit(1)
    else:
        if "pr-documentation" in pr_info.labels or "pr-doc-fix" in pr_info.labels:
            commit.create_status(
                context=NAME,
                description="Skipping checks for documentation",
                state="success",
                target_url=url,
            )
            print("::notice ::Can run, but it's documentation PR, skipping")
        else:
            print("::notice ::Can run")
            commit.create_status(
                context=NAME, description=description, state="pending", target_url=url
            )
