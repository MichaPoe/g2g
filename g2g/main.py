import logging
import logging.config
import json
from pathlib import Path

# http_client.HTTPConnection.debuglevel = 1

def setup_logging():
    with open('config/logging.json') as f:
        config = json.load(f)
    Path("logs").mkdir(exist_ok=True)
    logging.config.dictConfig(config)

setup_logging()

import click
import os
from datetime import datetime
import shutil
from g2g.gitlab_utils import download_group_repos, create_and_upload_to_new_instance, find_git_repos, stripped_values_of

logger = logging.getLogger(__name__)

@click.group()
def cli():
    pass

@cli.command()
@click.option('--api-url', help='The original GitLab API URL', required=True)
@click.option('--token', help='The GitLab Private Token', required=False)
@click.option('--group', help='The GitLab group to download', required=True)
@click.option('--output-file', default='repo_info.json', help='Output JSON file for repo information')
@click.option('--include', help='comma delimited list of glob patterns of paths to projects or groups to include', required=False)
@click.option('--exclude', help='comma delimited list of glob patterns of paths to projects or groups to exclude', required=False)
@click.option('--clean-all', is_flag=True, help='Remove all existing repos before download')
def download(api_url: str, token: str, group: str, output_file: str, include: str, exclude: str, clean_all: bool):
    logger.info("### Starting download")

    if not token:
        token = click.prompt('Please enter your GitLab Private Token', hide_input=True)

    if clean_all and os.path.exists(group):
        logger.info("Removing existing group directory: %s", group)
        shutil.rmtree(group)

    if not os.path.exists(group):
        os.makedirs(group)

    if output_file:
        backup_file_name = output_file
    else:
        timestamp = datetime.now().strftime('%Y%m%d')
        group_for_filename = group.replace("/", "_")
        backup_file_name = f"migration_{group_for_filename}_{timestamp}.json"

    includes = stripped_values_of(include)
    excludes = stripped_values_of(exclude)

    with open(backup_file_name, 'w') as f:
        group_and_project_info = download_group_repos(api_url, token, group, includes, excludes)
        json.dump(group_and_project_info, f)

    logger.info("### Finished download")


@cli.command()
@click.option('--api-url', help='The new GitLab API URL', required=True)
@click.option('--token', help='The GitLab Private Token for the new instance', required=False)
@click.option('--group', help='The GitLab group to upload to', required=False)
@click.option('--input-file', default='repo_info.json', help='Input JSON file for repo information', required=False)
def upload(api_url: str, token: str, group: str, input_file: str):
    logger.info("### Starting upload")

    if not token:
        token = click.prompt('Please enter your GitLab Private Token for the new instance', hide_input=True)

    if input_file and os.path.exists(input_file):
        with open(input_file, 'r') as f:
            repo_info = json.load(f)
        create_and_upload_to_new_instance(api_url, token, repo_info, group)
    else:
        if group and os.path.exists(group):
            repo_info = {}
            find_git_repos(group, repo_info)
            if repo_info:
                logger.info("Found git repos: %s", json.dumps(repo_info, indent=4))
                create_and_upload_to_new_instance(api_url, token, {"group_info": repo_info}, group)
            else:
                logger.warn("No git repositories found in folder %s", group)
        else:
            logger.error("Either specify an input JSON file or ensure the specified group folder %s exists.", group)

    logger.info("### Finished upload")

if __name__ == '__main__':
    cli()
