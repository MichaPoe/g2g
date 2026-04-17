import requests
import json
import urllib.parse
import os
import logging
from git import Repo, RemoteProgress
from git.exc import InvalidGitRepositoryError
from git import GitCommandError

logger = logging.getLogger(__name__)

class MyProgressPrinter(RemoteProgress):
    def update(self, op_code, cur_count, max_count=None, message=''):
        print(self._cur_line)

def download_group_repos(api_url, group, token):
    group_info = {}
    page = 1

    while True:
        response = requests.get(f"{api_url}/groups/{urllib.parse.quote_plus(group)}/projects", headers={"Private-Token": token}, params={"per_page": 100, "page": page})
        if response.status_code != 200:
            logger.error("Failed to get projects for group %s. Response: %s", group, response.text)
            return group_info

        projects = json.loads(response.text)
        if not projects:
            break

        for project in projects:
            repo_url = project['http_url_to_repo']
            repo_name = project['name']

            repo_url_parts = list(urllib.parse.urlsplit(repo_url))
            repo_url_parts[1] = f"oauth2:{token}@{urllib.parse.urlsplit(repo_url).netloc}"
            repo_url_with_token = urllib.parse.urlunsplit(repo_url_parts)

            try:
                logger.info("Cloning all branches of %s", repo_name)
                repo = Repo.clone_from(repo_url_with_token, f"{group}/{repo_name}", multi_options=['--mirror'], no_single_branch=True)
                group_info[repo_name] = {"url": repo_url, "path": f"{group}/{repo_name}"}
            except GitCommandError as e:
                logger.error("Failed to clone %s", repo_name, e)

        page += 1
    download_subgroups(api_url, group, token, group_info)
    return group_info

def download_subgroups(api_url, parent_group, token, group_info):
    page = 1
    while True:
        response = requests.get(f"{api_url}/groups/{urllib.parse.quote_plus(parent_group)}/subgroups", headers={"Private-Token": token}, params={"per_page": 100, "page": page})
        if response.status_code != 200:
            logger.error("Failed to get subgroups for group %s. Response: %s", parent_group, response.text)
            return

        subgroups = json.loads(response.text)
        if not subgroups:
            break

        for subgroup in subgroups:
            subgroup_name = subgroup['name']
            subgroup_path = subgroup['full_path']
            logger.info("Downloading subgroup %s", subgroup_name)

            os.makedirs(subgroup_path, exist_ok=True)
            subgroup_info = download_group_repos(api_url, subgroup_path, token)
            
            group_info.update(subgroup_info)

        page += 1

def create_or_get_group(api_url, token, group_name, parent_id=None):
    params = {}
    if parent_id:
        params['parent_id'] = parent_id
    response = requests.get(f"{api_url}/groups", headers={"Private-Token": token}, params=params)
    if response.status_code == 200:
        groups = json.loads(response.text)
        for group in groups:
            if group['name'] == group_name:
                logger.debug("Found existing group %s for parent ID %s with id %s", group_name, parent_id, group['id'])
                return group['id']
    logger.debug("Could not find existing group %s for parent ID %s in returned groups %s", group_name, parent_id, response.text)
    
    # Check for existence under the parent group, if parent_id is given
    if parent_id:
        response = requests.get(f"{api_url}/groups/{parent_id}/subgroups", headers={"Private-Token": token})
        if response.status_code == 200:
            subgroups = json.loads(response.text)
            for subgroup in subgroups:
                if subgroup['name'] == group_name:
                    logger.debug("Found existing subgroup %s for parent ID %s with id %s", group_name, parent_id, subgroup['id'])
                    return subgroup['id']
        logger.debug("Could not find existing subgroup %s for parent ID %s in returned groups %s", group_name, parent_id, response.text)

    sanitized_group_name = group_name.replace(" ", "_").replace("-", "_").lower()
    payload = {"name": group_name, "path": sanitized_group_name}
    if parent_id:
        payload['parent_id'] = parent_id

    response = requests.post(f"{api_url}/groups", headers={"Private-Token": token}, json=payload)
    if response.status_code == 201:
        return json.loads(response.text)['id']
    else:
        logger.error("Failed to create group %s. Response: %s", group_name, response.text)
        return None

def create_and_upload_to_new_instance(api_url, token, repo_info, group=None):
    for repo_name, repo_data in repo_info['group_info'].items():
        repo_path_parts = repo_data['path'].split("/")
        logger.info("Processing %s with path parts: %s", repo_name, repo_path_parts)

        if group:
            group_parts = group.split("/")
            if all(x not in repo_path_parts for x in group_parts):
                repo_path_parts = group_parts + repo_path_parts
            logger.info("Group specified. Updated path parts: %s", repo_path_parts)

        parent_id = None
        for part in repo_path_parts[:-1]:
            logger.info("Creating or getting group: %s", part)
            parent_id = create_or_get_group(api_url, token, part, parent_id)
            if parent_id is None:
                return
            logger.info("Group %s created or fetched with ID: %s", part, parent_id)

        sanitized_repo_name = repo_name.replace(" ", "_").replace("-", "_").lower()
        payload = {"name": repo_name, "path": sanitized_repo_name}
        
        if parent_id:
            payload['namespace_id'] = parent_id

        logger.info("Creating new project: %s under parent ID: %s", repo_name, parent_id)

        response = requests.post(f"{api_url}/projects", headers={"Private-Token": token}, json=payload)
        if response.status_code == 201:
            new_repo_url = json.loads(response.text)['http_url_to_repo']
        else:
            logger.warn("Failed to create project %s. Trying to fetch existing one. Response: %s", repo_name, response.text)
            # Fetch the existing project URL
            existing_project_response = requests.get(f"{api_url}/projects/{urllib.parse.quote_plus(repo_name)}", headers={"Private-Token": token})
            if existing_project_response.status_code != 200:
                logger.error("Failed to get existing project %s. Response: %s", repo_name, existing_project_response.text)
                continue
            new_repo_url = json.loads(existing_project_response.text)['http_url_to_repo']
        
        repo_path = repo_data['path']
        repo = Repo(repo_path)


        # Construct new remote URL with the token
        new_repo_url_parts = list(urllib.parse.urlsplit(new_repo_url))
        new_repo_url_parts[1] = f"oauth2:{token}@{urllib.parse.urlsplit(new_repo_url).netloc}"
        new_repo_url_with_token = urllib.parse.urlunsplit(new_repo_url_parts)

        # Add new remote
        new_remote_name = "new-origin"
        repo.create_remote(new_remote_name, url=new_repo_url_with_token)

        # Configurer le suivi des branches pour le nouveau remote
        for branch in repo.branches:
            # Configurer chaque branche locale pour suivre la même branche sur le nouveau remote
            branch_name = branch.name
            try:
                branch.set_tracking_branch(repo.remotes[new_remote_name].refs[branch_name])
                logger.info("Branch %s set to track %s/%s", branch_name, new_remote_name, branch_name)
            except IndexError:
                logger.error("Remote branch %s/%s does not exist. Skipping.", new_remote_name, branch_name)

        # Pousser toutes les branches et tags au nouveau remote
        try:
            logger.info("Pushing all branches and tags of %s to %s", repo_name, new_repo_url_with_token)
            repo.git.push(new_remote_name, '--all')
            repo.git.push(new_remote_name, '--tags')
            logger.info("Successfully pushed all branches and tags of %s", repo_name)
        except GitCommandError as e:
            logger.error("Failed to push repository %s", repo_name, e)

def find_git_repos(path, repo_info):
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