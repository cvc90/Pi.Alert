#!/usr/bin/env python

import os
import pathlib
import argparse
import sys
import hashlib
import csv
import sqlite3
from io import StringIO
from datetime import datetime

# Register NetAlertX directories
INSTALL_PATH="/app"
sys.path.extend([f"{INSTALL_PATH}/front/plugins", f"{INSTALL_PATH}/server"])

from plugin_helper import Plugin_Object, Plugin_Objects, decodeBase64
from logger import mylog, append_line_to_file
from helper import timeNowTZ, get_setting_value
from const import logPath, applicationPath, fullDbPath
import conf
from pytz import timezone

# Make sure the TIMEZONE for logging is correct
conf.tz = timezone(get_setting_value('TIMEZONE'))

CUR_PATH = str(pathlib.Path(__file__).parent.resolve())
LOG_FILE = os.path.join(CUR_PATH, 'script.log')
RESULT_FILE = os.path.join(CUR_PATH, 'last_result.log')

pluginName = 'DBCLNP'

def main():
    
    parser = argparse.ArgumentParser(description='DB cleanup tasks')
    parser.add_argument('pluginskeephistory', action="store", help="TBC")
    parser.add_argument('hourstokeepnewdevice', action="store", help="TBC")
    parser.add_argument('daystokeepevents', action="store", help="TBC")
    parser.add_argument('pholuskeepdays', action="store", help="TBC") # unused
    
    values = parser.parse_args()

    PLUGINS_KEEP_HIST       = int(values.pluginskeephistory.split('=')[1])
    HRS_TO_KEEP_NEWDEV      = int(values.hourstokeepnewdevice.split('=')[1])
    DAYS_TO_KEEP_EVENTS     = int(values.daystokeepevents.split('=')[1])
    PHOLUS_DAYS_DATA        = get_setting_value("PHOLUS_DAYS_DATA")

    mylog('verbose', [f'[{pluginName}] In script'])     


    # Execute cleanup/upkeep    
    cleanup_database(fullDbPath, DAYS_TO_KEEP_EVENTS, PHOLUS_DAYS_DATA, HRS_TO_KEEP_NEWDEV, PLUGINS_KEEP_HIST)
    
    mylog('verbose', [f'[{pluginName}] Cleanup complete'])   
    
    return 0

#===============================================================================
# Cleanup / upkeep database
#===============================================================================
def cleanup_database (dbPath, DAYS_TO_KEEP_EVENTS, PHOLUS_DAYS_DATA, HRS_TO_KEEP_NEWDEV, PLUGINS_KEEP_HIST):
    """
    Cleaning out old records from the tables that don't need to keep all data.
    """

    mylog('verbose', [f'[{pluginName}] Upkeep Database:' ])

    # Connect to the App database
    conn    = sqlite3.connect(dbPath)
    cursor  = conn.cursor()

    # -----------------------------------------------------
    # Cleanup Online History
    mylog('verbose', [f'[{pluginName}] Online_History: Delete all but keep latest 150 entries'])
    cursor.execute ("""DELETE from Online_History where "Index" not in (
                            SELECT "Index" from Online_History 
                            order by Scan_Date desc limit 150)""")
    

    # -----------------------------------------------------
    # Cleanup Events
    mylog('verbose', [f'[{pluginName}] Events: Delete all older than {str(DAYS_TO_KEEP_EVENTS)} days (DAYS_TO_KEEP_EVENTS setting)'])
    cursor.execute (f"""DELETE FROM Events 
                            WHERE eve_DateTime <= date('now', '-{str(DAYS_TO_KEEP_EVENTS)} day')""")
    # -----------------------------------------------------   
    # Trim Plugins_History entries to less than PLUGINS_KEEP_HIST setting per unique "Plugin" column entry
    mylog('verbose', [f'[{pluginName}] Plugins_History: Trim Plugins_History entries to less than {str(PLUGINS_KEEP_HIST)} per Plugin (PLUGINS_KEEP_HIST setting)'])

    # Build the SQL query to delete entries that exceed the limit per unique "Plugin" column entry
    delete_query = f"""DELETE FROM Plugins_History 
                            WHERE "Index" NOT IN (
                                SELECT "Index"
                                FROM (
                                    SELECT "Index", 
                                        ROW_NUMBER() OVER(PARTITION BY "Plugin" ORDER BY DateTimeChanged DESC) AS row_num
                                    FROM Plugins_History
                                ) AS ranked_objects
                                WHERE row_num <= {str(PLUGINS_KEEP_HIST)}
                            );"""

    cursor.execute(delete_query)

    # -----------------------------------------------------
    # Trim Notifications entries to less than DBCLNP_NOTIFI_HIST setting

    histCount = get_setting_value('DBCLNP_NOTIFI_HIST')

    mylog('verbose', [f'[{pluginName}] Plugins_History: Trim Notifications entries to less than {histCount}'])

    # Build the SQL query to delete entries 
    delete_query = f"""DELETE FROM Notifications 
                            WHERE "Index" NOT IN (
                               SELECT "Index"
                                        FROM (
                                            SELECT "Index", 
                                                ROW_NUMBER() OVER(PARTITION BY "Notifications" ORDER BY DateTimeCreated DESC) AS row_num
                                            FROM Notifications
                                        ) AS ranked_objects
                                        WHERE row_num <= {histCount}
                            );"""

    cursor.execute(delete_query)


    # -----------------------------------------------------
    # Trim Workflow entries to less than WORKFLOWS_AppEvents_hist setting
    histCount = get_setting_value('WORKFLOWS_AppEvents_hist')

    mylog('verbose', [f'[{pluginName}] Trim AppEvents to less than {histCount}'])

    # Build the SQL query to delete entries 
    delete_query = f"""DELETE FROM AppEvents 
                            WHERE "Index" NOT IN (
                               SELECT "Index"
                                        FROM (
                                            SELECT "Index", 
                                                ROW_NUMBER() OVER(PARTITION BY "AppEvents" ORDER BY DateTimeCreated DESC) AS row_num
                                            FROM AppEvents
                                        ) AS ranked_objects
                                        WHERE row_num <= {histCount}
                            );"""

    cursor.execute(delete_query)


    # -----------------------------------------------------
    # Cleanup New Devices
    if HRS_TO_KEEP_NEWDEV != 0:
        mylog('verbose', [f'[{pluginName}] Devices: Delete all New Devices older than {str(HRS_TO_KEEP_NEWDEV)} hours (HRS_TO_KEEP_NEWDEV setting)'])            
        query = f"""DELETE FROM Devices WHERE dev_NewDevice = 1 AND dev_FirstConnection < date('now', '-{str(HRS_TO_KEEP_NEWDEV)} hour')"""
        mylog('verbose', [f'[{pluginName}] Query: {query} '])            
        cursor.execute (query) 

    # -----------------------------------------------------
    # Cleanup Pholus_Scan
    if PHOLUS_DAYS_DATA != "" and PHOLUS_DAYS_DATA != 0:
        mylog('verbose', [f'[{pluginName}] Pholus_Scan: Delete all older than ' + str(PHOLUS_DAYS_DATA) + ' days (PHOLUS_DAYS_DATA setting)'])
        # todo: improvement possibility: keep at least N per mac
        cursor.execute (f"""DELETE FROM Pholus_Scan 
                                WHERE Time <= date('now', '-{str(PHOLUS_DAYS_DATA)} day')""") 

    
    
    # -----------------------------------------------------
    # De-Dupe (de-duplicate - remove duplicate entries) from the Pholus_Scan table
    mylog('verbose', [f'[{pluginName}] Pholus_Scan: Delete all duplicates'])
    cursor.execute ("""DELETE  FROM Pholus_Scan
                    WHERE rowid > (
                    SELECT MIN(rowid) FROM Pholus_Scan p2
                    WHERE Pholus_Scan.MAC = p2.MAC
                    AND Pholus_Scan.Value = p2.Value
                    AND Pholus_Scan.Record_Type = p2.Record_Type
                    );""")


    # -----------------------------------------------------
    # De-dupe (de-duplicate) from the Plugins_Objects table 
    # TODO This shouldn't be necessary - probably a concurrency bug somewhere in the code :(        
    mylog('verbose', [f'[{pluginName}] Plugins_Objects: Delete all duplicates'])
    cursor.execute("""
        DELETE FROM Plugins_Objects
        WHERE rowid > (
            SELECT MIN(rowid) FROM Plugins_Objects p2
            WHERE Plugins_Objects.Plugin = p2.Plugin
            AND Plugins_Objects.Object_PrimaryID = p2.Object_PrimaryID
            AND Plugins_Objects.Object_SecondaryID = p2.Object_SecondaryID
            AND Plugins_Objects.UserData = p2.UserData
        )
    """)


    conn.commit()

    # Shrink DB
    mylog('verbose', [f'[{pluginName}] Shrink Database'])
    cursor.execute ("VACUUM;")

    # Close the database connection
    conn.close()
    
    

#===============================================================================
# BEGIN
#===============================================================================
if __name__ == '__main__':
    main()