import os
from git import Repo
from dotenv import load_dotenv

load_dotenv()

def push_to_github(file_path, commit_message):
    try:
        repo = Repo(os.getcwd())
        repo.index.add([file_path])
        repo.index.commit(commit_message)
        origin = repo.remote(name='origin')
        origin.push()
        print(f"Successfully pushed {file_path} to GitHub.")
    except Exception as e:
        print(f"Error pushing to GitHub: {e}")