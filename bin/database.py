#!/usr/bin/env python

import bson
import copy
import dateutil.parser
import json
import logging
import sys

from api import config

CURRENT_DATABASE_VERSION = 7 # An int that is bumped when a new schema change is made

def get_db_version():

    version = config.get_version()
    if version is None or version.get('database', None) is None:
        return 0
    else:
        return version.get('database')


def confirm_schema_match():
    """
    Checks version of database schema

    Returns (0)  if DB schema version matches requirements.
    Returns (42) if DB schema version does not match
                 requirements and can be upgraded.
    Returns (43) if DB schema version does not match
                 requirements and cannot be upgraded,
                 perhaps because code is at lower version
                 than the DB schema version.
    """

    db_version = get_db_version()
    if not isinstance(db_version, int) or db_version > CURRENT_DATABASE_VERSION:
        logging.error('The stored db schema version of %s is incompatible with required version %s',
                       str(db_version), CURRENT_DATABASE_VERSION)
        sys.exit(43)
    elif db_version < CURRENT_DATABASE_VERSION:
        sys.exit(42)
    else:
        sys.exit(0)

def upgrade_to_1():
    """
    scitran/core issue #206

    Initialize db version to 1
    """
    config.db.version.insert_one({'_id': 'version', 'database': 1})

def upgrade_to_2():
    """
    scitran/core PR #236

    Set file.origin.name to id if does not exist
    Set file.origin.method to '' if does not exist
    """

    def update_file_origins(cont_list, cont_name):
        for container in cont_list:
            updated_files = []
            for file in container.get('files', []):
                origin = file.get('origin')
                if origin is not None:
                    if origin.get('name', None) is None:
                        file['origin']['name'] = origin['id']
                    if origin.get('method', None) is None:
                        file['origin']['method'] = ''
                updated_files.append(file)

            query = {'_id': container['_id']}
            update = {'$set': {'files': updated_files}}
            result = config.db[cont_name].update_one(query, update)

    query = {'$and':[{'files.origin.name': { '$exists': False}}, {'files.origin.id': { '$exists': True}}]}

    update_file_origins(config.db.collections.find(query), 'collections')
    update_file_origins(config.db.projects.find(query), 'projects')
    update_file_origins(config.db.sessions.find(query), 'sessions')
    update_file_origins(config.db.acquisitions.find(query), 'acquisitions')

def upgrade_to_3():
    """
    scitran/core issue #253

    Set first user with admin permissions found as curator if one does not exist
    """
    query = {'curator': {'$exists': False}, 'permissions.access': 'admin'}
    projection = {'permissions.$':1}
    collections = config.db.collections.find(query, projection)
    for coll in collections:
        admin = coll['permissions'][0]['_id']
        query = {'_id': coll['_id']}
        update = {'$set': {'curator': admin}}
        config.db.collections.update_one(query, update)

def upgrade_to_4():
    """
    scitran/core issue #263

    Add '_id' field to session.subject
    Give subjects with the same code and project the same _id
    """

    pipeline = [
        {'$match': { 'subject._id': {'$exists': False}}},
        {'$group' : { '_id' : {'pid': '$project', 'code': '$subject.code'}, 'sids': {'$push': '$_id' }}}
    ]

    subjects = config.db.command('aggregate', 'sessions', pipeline=pipeline)
    for subject in subjects['result']:

        # Subjects without a code and sessions without a subject
        # will be returned grouped together, but all need unique IDs
        if subject['_id'].get('code') is None:
            for session_id in subject['sids']:
                subject_id = bson.ObjectId()
                config.db.sessions.update_one({'_id': session_id},{'$set': {'subject._id': subject_id}})
        else:
            subject_id = bson.ObjectId()
            query = {'_id': {'$in': subject['sids']}}
            update = {'$set': {'subject._id': subject_id}}
            config.db.sessions.update_many(query, update)

def upgrade_to_5():
    """
    scitran/core issue #279

    Ensure all sessions and acquisitions have the same perms as their project
    Bug(#278) discovered where changing a session's project did not update acquisition perms
    """

    projects = config.db.projects.find({})
    for p in projects:
        perms = p.get('permissions', [])

        session_ids = [s['_id'] for s in config.db.sessions.find({'project': p['_id']}, [])]

        config.db.sessions.update_many({'project': p['_id']}, {'$set': {'permissions': perms}})
        config.db.acquisitions.update_many({'session': {'$in': session_ids}}, {'$set': {'permissions': perms}})

def upgrade_to_6():
    """
    scitran/core issue #277

    Ensure all collection modified dates are ISO format
    Bug fixed in 6967f23
    """

    colls = config.db.collections.find({'modified': {'$type': 2}}) # type string
    for c in colls:
        fixed_mod = dateutil.parser.parse(c['modified'])
        config.db.collections.update_one({'_id': c['_id']}, {'$set': {'modified': fixed_mod}})

def upgrade_to_7():
    """
    scitran/core issue #270

    Add named inputs and specified destinations to jobs.

    Before:
    {
        "input" : {
            "container_type" : "acquisition",
            "container_id" : "572baf4e23dcb77ebbe06b3f",
            "filename" : "1_1_dicom.zip",
            "filehash" : "v0-sha384-422bd115d21585d1811d42cd99f1cf0a8511a4b377dd2deeaa1ab491d70932a051926ed99815a75142ad0815088ed009"
        }
    }

    After:
    {
        "inputs" : {
            "dicom" : {
                "container_type" : "acquisition",
                "container_id" : "572baf4e23dcb77ebbe06b3f",
                "filename" : "1_1_dicom.zip"
            }
        },
        "destination" : {
            "container_type" : "acquisition",
            "container_id" : "572baf4e23dcb77ebbe06b3f"
        }
    }
    """

    # The infrastructure runs this upgrade script before populating manifests.
    # For this reason, this one-time script does NOT pull manifests to do the input-name mapping, instead relying on a hard-coded alg name -> input name map.
    # If you have other gears in your system at the time of upgrade, you must add that mapping here.
    input_name_for_gear = {
        'dcm_convert': 'dicom',
        'qa-report-fmri': 'nifti',
        'dicom_mr_classifier': 'dicom',
    }

    jobs = config.db.jobs.find({})

    for job in jobs:
        gear_name = job['algorithm_id']
        input_name = input_name_for_gear[gear_name]

        # # Move single input to named input map
        input = job['input']
        input.pop('filehash', None)
        inputs = { input_name: input }

        # # Destination is required, and (for these jobs) is always the same container as the input
        destination = copy.deepcopy(input)
        destination.pop('filename', None)

        config.db.jobs.update_one(
            {'_id': job['_id']},
            {
                '$set': {
                    'inputs': inputs,
                    'destination': destination
                },
                '$unset': {
                    'input': ''
                }
            }
        )

def upgrade_to_8():
    """
    scitran/core issue #189 - Data Model v2

    Field `metadata` renamed to `info`
    Field `file.instrument` renamed to `file.modality`
    Acquisition fields `instrument` and `measurement` removed
    """

    def dm_v2_updates(cont_list, cont_name):
        for container in cont_list:

            query = {'_id': container['_id']}
            update = {'$rename': {'metadata': 'info'}}

            if cont_name == 'sessions':
                update['$rename'].update({'subject.metadata': 'subject.info'})

            if cont_name == 'acquisitions':
                update['$unset'] = {'instrument': '', 'measurements': ''}

            # From mongo docs: '$rename does not work if these fields are in array elements.'
            files = container.get('files')
            if files is not None:
                updated_files = []
                for file_ in files:
                    if file_.get('metadata') is not None:
                        file_['info'] = file_.pop('metadata')
                    if file_.get('instrument') is not None:
                        file_['modality'] = file_.pop('instrument')
                    updated_files.append(file_)
                update['$set'] = {'files': updated_files}

            result = config.db[cont_name].update_one(query, update)

    query = {'$or':[{'files': { '$exists': True}},
                    {'subject': { '$exists': True}},
                    {'metadata': { '$exists': True}}]}

    dm_v2_updates(config.db.collections.find(query), 'collections')
    dm_v2_updates(config.db.projects.find(query), 'projects')
    dm_v2_updates(config.db.sessions.find(query), 'sessions')

    query['$or'].append({'instrument': { '$exists': True}})
    query['$or'].append({'measurement': { '$exists': True}})
    dm_v2_updates(config.db.acquisitions.find(query), 'acquisitions')

def upgrade_schema():
    """
    Upgrades db to the current schema version

    Returns (0) if upgrade is successful
    """

    db_version = get_db_version()
    try:
        if db_version < 1:
            upgrade_to_1()
        if db_version < 2:
            upgrade_to_2()
        if db_version < 3:
            upgrade_to_3()
        if db_version < 4:
            upgrade_to_4()
        if db_version < 5:
            upgrade_to_5()
        if db_version < 6:
            upgrade_to_6()
        if db_version < 7:
            upgrade_to_7()
        if db_version < 8:
            upgrade_to_8()

    except Exception as e:
        logging.exception('Incremental upgrade of db failed')
        sys.exit(1)
    else:
        config.db.version.update_one({'_id': 'version'}, {'$set': {'database': CURRENT_DATABASE_VERSION}})
        sys.exit(0)

try:
    if len(sys.argv) > 1:
        if sys.argv[1] == 'confirm_schema_match':
            confirm_schema_match()
        elif sys.argv[1] == 'upgrade_schema':
            upgrade_schema()
        else:
            logging.error('Unknown method name given as argv to database.py')
            sys.exit(1)
    else:
        logging.error('No method name given as argv to database.py')
        sys.exit(1)
except Exception as e:
    logging.exception('Unexpected error in database.py')
    sys.exit(1)
