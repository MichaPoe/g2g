import requests
import json
import urllib.parse
import os
import fnmatch
import logging
from git import Repo, RemoteProgress
from git.exc import InvalidGitRepositoryError
from git import GitCommandError

logger = logging.getLogger(__name__)

class MyProgressPrinter(RemoteProgress):
    def update(self, op_code, cur_count, max_count=None, message=''):
        print(self._cur_line)

def perform_paged_get(api_url: str, token: str, params: dict = None) -> list:
    """
    perform_paged_get

    :param api_url: url to target Gitlab API
    :param token: token for authentication
    :param params: optional request parameters
    :return:
    """
    auth_headers={"Private-Token": token}
    page_size=100
    results=[]
    page = 1
    while True:
        all_params = {"per_page": page_size, "page": page}
        if params:
            all_params.update(params)

        response = requests.get(api_url, headers=auth_headers, params=all_params)
        logger.debug("Performed get %s on page %d returned %d", api_url, page, response.status_code)
        if response.status_code != 200:
            logger.warn("Failed to perform get %s with params %s. Response: %d - %s", api_url, all_params, response.status_code, response.text)
            break

        data = response.json()
        if not data:  # No more data
            break

        results.extend(data)

        # Stop if we got less than page_size (last page)
        if len(data) < page_size:
            break

        page += 1

    logger.debug("Performed get %s on %d page(s) returning %d entries", api_url, page, len(results))
    return results

def stripped_values_of(values: str) -> list:
    return [pattern.strip() for pattern in values.split(',')] if values else None

def should_process(path_with_namespace: str, includes: list, excludes: list) -> bool:
    """
    should_process checks whether project given by its full path should be processed due to given optional includes and excludes

    :param path_with_namespace: full path to project
    :param includes: list of glob patterns of paths to projects or groups to include
    :param excludes: list of glob patterns of paths to projects or groups to exclude
    :return: flag indicating whether project should be processed or not
    """

    if excludes:
        exclude_matches = any(fnmatch.fnmatch(path_with_namespace, pattern) for pattern in excludes)
        if exclude_matches:
            logger.debug("exclude matches path %s - NOT processing", path_with_namespace)
            return False

    if includes:
        include_matches = any(fnmatch.fnmatch(path_with_namespace, pattern) for pattern in includes)
        if not include_matches:
            logger.debug("include do not matches path %s - NOT processing", path_with_namespace)
            return False
        else:
            logger.debug("include matches path %s - processing", path_with_namespace)
            return True

    logger.debug("no includes and no excludes provided to match path %s - processing", path_with_namespace)
    return True

def download_group_repos(api_url: str, token: str, group_name: str, includes: list, excludes: list) -> dict:
    """
    download_group_repos download all repositories of group matching optional includes or/and excludes
    If neither includes nor excludes are provided all repositories will be downloaded.
    If an include and an exclude matches, exclude wins.

    :param api_url: url to target Gitlab API
    :param token: token for authentication
    :param group_name: group to download repositories for
    :param includes: list of glob patterns of paths to projects or groups to include
    :param excludes: list of glob patterns of paths to projects or groups to exclude
    :return: group and project info
    """
    group_and_project_info = { "projects": {}, "groups": {} }

    group_info_url = f"{api_url}/groups/{urllib.parse.quote_plus(group_name)}?with_projects=false"
    response = requests.get(group_info_url, headers={"Private-Token": token})
    if response.status_code != 200:
        logger.warn("Failed to perform get %s. Response: %d - %s", group_info_url, response.status_code, response.text)
        return group_and_project_info
    project = response.json()
    group_and_project_info["groups"][group_name] = {"name": project["name"], "description": project["description"]}

    projects = perform_paged_get(f"{api_url}/groups/{urllib.parse.quote_plus(group_name)}/projects", token)
    if len(projects) <= 0:
        logger.info("No projects for group %s", group_name)
        return group_and_project_info

    for project in projects:
        path_with_namespace = project['path_with_namespace']
        if not should_process(path_with_namespace, includes, excludes):
            logger.info("Skipping project %s", path_with_namespace)
            continue

        # make group directory
        os.makedirs(group_name, exist_ok=True)

        repo_url = project['http_url_to_repo']
        repo_name = project['name']

        repo_url_parts = list(urllib.parse.urlsplit(repo_url))
        repo_url_parts[1] = f"oauth2:{token}@{urllib.parse.urlsplit(repo_url).netloc}"
        repo_url_with_token = urllib.parse.urlunsplit(repo_url_parts)

        try:
            logger.info("Cloning all branches of %s", repo_name)
            repo_full_name = f"{group_name}/{repo_name}"
            repo = Repo.clone_from(repo_url_with_token, repo_full_name, multi_options=['--mirror'], no_single_branch=True)
            # TODO we do not need url in any place at all
            group_and_project_info["projects"][repo_full_name] = {"name": repo_name, "url": repo_url}
        except GitCommandError as e:
            logger.error("Failed to clone %s", repo_name, e)

    download_subgroups(api_url, token, group_name, group_and_project_info, includes, excludes)

    return group_and_project_info

def download_subgroups(api_url: str, token: str, parent_group_name, group_and_project_info: dict, includes: list, excludes: list):
    """
    download_subgroups download all repositories of subgroups and update group info

    :param api_url: url to target Gitlab API
    :param token: token for authentication
    :param parent_group_name: parent group to download repositories for
    :param group_and_project_info: group and project info
    :param includes: list of glob patterns of paths to projects or groups to include
    :param excludes: list of glob patterns of paths to projects or groups to exclude
    """
    subgroups = perform_paged_get(f"{api_url}/groups/{urllib.parse.quote_plus(parent_group_name)}/subgroups", token)
    if len(subgroups) <= 0:
        logger.info("No subgroups for parent group %s", parent_group_name)
        return

    for subgroup in subgroups:
        subgroup_name = subgroup['name']
        subgroup_path = subgroup['full_path']
        logger.info("Downloading subgroup %s", subgroup_name)

        subgroup_info = download_group_repos(api_url, token, subgroup_path, includes, excludes)

        group_and_project_info["projects"].update(subgroup_info["projects"])
        group_and_project_info["groups"].update(subgroup_info["groups"])

    # TODO perform subgroups of subgroups in case of nested subgroups

def create_or_get_group(api_url, token, group_name, parent_id=None) -> str:
    """
    create_or_get_group ensures group exists in target

    :param api_url: url to target Gitlab API
    :param token: token for authentication
    :param group_name: name of group to ensure
    :param parent_id: optional id of parent group where group belongs
    :return: id of ensured group
    """
    params = {}
    if parent_id:
        params['parent_id'] = parent_id

    # Check for existence as root group
    # TODO params should not be needed
    groups = perform_paged_get(f"{api_url}/groups", token, params)
    for group in groups:
        if group['name'] == group_name:
            logger.debug("Found existing group %s for parent ID %s with id %s", group_name, parent_id, group['id'])
            return group['id']
    found_group_names = [group['name'] for group in groups]
    logger.debug("Could not find existing group %s for parent ID %s in %d - returned group names %s", group_name, parent_id, len(found_group_names), found_group_names)

    # Check for existence under the parent group, if parent_id is given
    if parent_id:
        subgroups = perform_paged_get(f"{api_url}/groups/{parent_id}/subgroups", token)
        for subgroup in subgroups:
            if subgroup['name'] == group_name:
                logger.debug("Found existing subgroup %s for parent ID %s with id %s", group_name, parent_id, subgroup['id'])
                return subgroup['id']
        found_subgroup_names = [subgroup['name'] for subgroup in subgroups]
        logger.debug("Could not find existing subgroup %s for parent ID %s with id %d - returned subgroup names %s", group_name, parent_id, len(found_subgroup_names), found_subgroup_names)

    # Create missing group
    sanitized_group_name = group_name.replace(" ", "_").replace("-", "_").lower()
    payload = {"name": group_name, "path": sanitized_group_name}
    if parent_id:
        payload['parent_id'] = parent_id

    response = requests.post(f"{api_url}/groups", headers={"Private-Token": token}, json=payload)
    if response.status_code == 201:
        return json.loads(response.text)['id']
    else:
        logger.error("Failed to create group %s. Response: %d - %s", group_name, response.status_code, response.text)
        return None

def create_and_upload_to_new_instance(api_url: str, token: str, repo_info: dict, group_name: str=None):
    """
    create_and_upload_to_new_instance create and upload project

    :param api_url: url to target Gitlab API
    :param token: token for authentication
    :param repo_info: repository information
    :param group_name: optional target group prefix
    """
    for repo_name, repo_data in repo_info['group_info'].items():
        repo_path_parts = repo_data['path'].split("/")
        logger.info("Processing %s with path parts: %s", repo_name, repo_path_parts)

        # prefix by group if provided
        if group_name:
            group_parts = group_name.split("/")
            if all(x not in repo_path_parts for x in group_parts):
                repo_path_parts = group_parts + repo_path_parts
            logger.info("Group specified. Updated path parts: %s", repo_path_parts)

        # create groups and subgroups
        parent_id = None
        for part in repo_path_parts[:-1]:
            logger.info("Creating or getting group: %s", part)
            parent_id = create_or_get_group(api_url, token, part, parent_id)
            if parent_id is None:
                return
            logger.info("Group %s created or fetched with ID: %s", part, parent_id)

        # ensure project
        sanitized_repo_name = repo_name.replace(" ", "_").replace("-", "_").lower()
        payload = {"name": repo_name, "path": sanitized_repo_name}
        
        if parent_id:
            payload['namespace_id'] = parent_id

        logger.info("Creating new project: %s under parent ID: %s", repo_name, parent_id)

        response = requests.post(f"{api_url}/projects", headers={"Private-Token": token}, json=payload)
        if response.status_code == 201:
            new_repo_url = json.loads(response.text)['http_url_to_repo']
        else:
            logger.warn("Failed to create project %s. Trying to fetch existing one. Response: %d - %s", repo_name, response.status_code, response.text)

            projects_url = None
            if parent_id:
                projects_url = f"{api_url}/groups/{parent_id}/projects"
            else:
                projects_url = f"{api_url}/projects"
            projects = perform_paged_get(projects_url, token)

            if len(projects) <= 0:
                logger.warn("Project %s not found for parent ID %s", repo_name, parent_id)
                continue

            new_repo_url = None
            for project in projects:
                if project['name'] == repo_name:
                    logger.debug("Found existing project %s for parent ID %s with id %s and url %s", repo_name, parent_id, project['id'], project['http_url_to_repo'])
                    new_repo_url = project['http_url_to_repo']

            if not new_repo_url:
                logger.warn("Project %s not found for parent ID %s in %d projects", repo_name, parent_id, len(projects))
                continue

        repo_path = repo_data['path']
        repo = Repo(repo_path)

        # upload project

        # Construct new remote URL with the authentication token
        new_repo_url_parts = list(urllib.parse.urlsplit(new_repo_url))
        new_repo_url_parts[1] = f"oauth2:{token}@{urllib.parse.urlsplit(new_repo_url).netloc}"
        new_repo_url_with_token = urllib.parse.urlunsplit(new_repo_url_parts)

        # Add new remote
        new_remote_name = "new-origin"
        repo.create_remote(new_remote_name, url=new_repo_url_with_token)

        # Configure branch tracking for the new remote
        for branch in repo.branches:
            # Configure each local branch to follow the same branch on the new remote
            try:
                branch.set_tracking_branch(repo.remotes[new_remote_name].refs[branch.name])
                logger.info("Branch %s set to track %s/%s", branch.name, new_remote_name, branch.name)
            except IndexError:
                logger.warn("Remote branch %s/%s does not exist.", new_remote_name, branch.name)

        # Push all branches and tags to the new remote
        try:
            logger.info("Pushing all branches and tags of %s to %s", repo_name, new_repo_url_with_token)
            repo.git.push(new_remote_name, '--all')
            repo.git.push(new_remote_name, '--tags')
            logger.info("Successfully pushed all branches and tags of %s", repo_name)
        except GitCommandError as e:
            logger.error("Failed to push repository %s", repo_name, e)

        # Delete new remote
        repo.delete_remote(new_remote_name)

def find_git_repos(path: str, repo_info: dict):
    """
    find_git_repos updates repo_info with found repositories under path

    :param path: path to search for git repositories
    :param repo_info: repository information found so far - will be updated
    """
    for folder in os.listdir(path):
        folder_path = os.path.join(path, folder)
        if os.path.isdir(folder_path):
            try:
                repo = Repo(folder_path)
                if repo.git_dir:
                    branches = [branch.name for branch in repo.branches]
                    repo_info[folder] = {
                        "path": folder_path,
                        "branches": branches
                    }
            except InvalidGitRepositoryError:
                find_git_repos(folder_path, repo_info)
