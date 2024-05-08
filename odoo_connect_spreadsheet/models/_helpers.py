"""
Some of the helpers below are sourced from: https://python.plainenglish.io/master-google-sheets-api-in-python-cheat-sheet-3535e86fbe17
"""
from apiclient import discovery
from google.oauth2 import service_account


def google_drive_authentication(scopes, service_account_json_credentials):
    credentials = service_account.Credentials.from_service_account_info(service_account_json_credentials, scopes=scopes)
    drive_service = discovery.build('drive', 'v3', credentials=credentials)
    return drive_service


def google_sheet_authentication(scopes, service_account_json_credentials):
    credentials = service_account.Credentials.from_service_account_info(service_account_json_credentials, scopes=scopes)
    spreadsheet_service = discovery.build('sheets', 'v4', credentials=credentials)
    return spreadsheet_service


def create_drive_folder(drive_service, folder_name):
    """
    Create a Drive Folder

    file_metadata = {
        'name': 'medium_spreadsheet_folder',
        'mimeType': 'application/vnd.google-apps.folder'
    }
    """
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder'
    }
    file = drive_service.files().create(body=file_metadata, fields='id').execute()
    folder_id = file.get('id')
    return folder_id


def update_folder_permission(drive_service, folder_id, email_address):
    """
    Update folder permissions

    new_permissions = {
        'type': 'group',
        'role': 'writer',
        'emailAddress': EMAIL_ADDRESS
    }
    """

    new_permissions = {
        'type': 'group',
        'role': 'writer',
        'emailAddress': email_address
    }  # todo | should we loop it to update multiple users and permissions?

    permission_response = drive_service.permissions().create(
        fileId=folder_id, body=new_permissions).execute()

    return permission_response


def create_spreadsheet(spreadsheet_service, spreadsheet_title):
    """
    Create a spreadsheet

    spreadsheet = {
        'properties': {
            'title': "medium_spreadsheet_file"
        }
    }
    """
    spreadsheet = {
        'properties': {
            'title': spreadsheet_title
        }
    }
    creation_response = spreadsheet_service.spreadsheets().create(body=spreadsheet, fields='spreadsheetId').execute()

    spreadsheet_id = creation_response.get('spreadsheetId')
    return spreadsheet_id


def update_spreadsheet(spreadsheet_service, spreadsheet_id, range_name, _values):
    """
    Update a spreadsheet

    range_name = "A1:C2"
    If the number of rows is variable, you can use this value in range_name :
    range_name = "A1:C{}".format(len(rows))

    values = [
        ["Medium Channel", "Views", "Likes"],
        ["Beranger", "'{}".format(10983908), '{}'.format(13084)]
    ]
    """

    data = {'values': _values}

    update_response = spreadsheet_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        body=data,
        range=range_name,
        valueInputOption='USER_ENTERED').execute()

    return update_response


def read_value_from_spreadsheet(spreadsheet_service, spreadsheet_id, range_name):
    """
    Read values from a spreadsheet

    range_name = "Sheet1!A1:C2"
    """
    response = spreadsheet_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_name
    ).execute()

    return response['values']


def update_spreadsheet_permission(drive_service, spreadsheet_id, type, role, email_address):
    """
    new_file_permission = {
        'type': 'group',
        'role': 'writer',
        'emailAddress': trinanda_example_email@gmail.com
    }
    """
    new_file_permission = {
        'type': type,
        'role': role,
        'emailAddress': email_address,
    }

    permission_response = drive_service.permissions().create(
        fileId=spreadsheet_id, body=new_file_permission).execute()

    return permission_response


def move_folder(drive_service, spreadsheet_id, folder_id):
    """
    Move to folder
    The previous update_spreadsheet_permission method is great if you have only a few spreadsheets to grant access to.
    But if you plan to create a huge number of spreadsheets, you will receive a huge number of email, You should
    instead use this method. We won’t update spreadsheet’s permissions but we will move the spreadsheet into a
    folder that already have the wanted permissions.
    Remember the folder we created using create_drive_folder method? We simply use his folder_id and the spreadsheet’s
    permissions will be automatically updated.
    """
    get_parents_response = drive_service.files().get(fileId=spreadsheet_id, fields='parents').execute()
    previous_parents = ",".join(get_parents_response.get('parents'))
    move_response = drive_service.files().update(fileId=spreadsheet_id, addParents=folder_id,
                                                 removeParents=previous_parents, fields='id, parents').execute()
    return move_response


def add_sheet(spreadsheet_service, spreadsheet_id, sheet_name):
    # Create a AddSheetRequest object:
    add_sheet_request = {
        "addSheet": {
            "properties": {
                "title": sheet_name
            }
        }
    }

    # Create a BatchUpdateSpreadsheetRequest object:
    request = {
        "requests": [add_sheet_request]
    }

    response = spreadsheet_service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=request).execute()
    return response


def spreadsheet_metadata(spreadsheet_service, spreadsheet_id):
    response = spreadsheet_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return response


def clear_spreadsheet_data(spreadsheet_service, spreadsheet_id, range_name):
    # Call the spreadsheets.values.clear method:
    result = spreadsheet_service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=range_name).execute()
    return result


def append_values(spreadsheet_service, spreadsheet_id, range_name, _values, insert_data_option, major_dimension):
    """Append values to spreadsheet"""
    data = {'values': _values, 'majorDimension': major_dimension}
    result = spreadsheet_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id, range=range_name,
        valueInputOption='USER_ENTERED', insertDataOption=insert_data_option,
        body=data).execute()
    return result
