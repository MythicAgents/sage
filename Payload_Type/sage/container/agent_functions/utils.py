import os
from mythic_container.MythicCommandBase import PTTaskMessageAllData
from mythic_container.MythicRPC import MythicRPCOperationEventLogCreateMessage, SendMythicRPCOperationEventLogCreate
from mythic_container.logging import logger

def get_secret(taskData: PTTaskMessageAllData, key: str) -> str|None:
    """
    Retrieves the value for a given key from the task, user secrets, payload build parameters, or payload container environment variables.
    It checks the following order:
    1. Task Level
    2. User Secrets
    3. Payload Build Parameters
    4. Payload Container Environment Variables
    If the key is not found in any of these, it returns None.

    Parameters:
    taskData: The Mythic task that is being processed
    key (str): The name of the key for the secret to get (e.g., OPENAI_API_KEY)

    Returns:
    str | None: the secrets if found or None if not found
    """
    # Check if the key exists in the task
    v = taskData.args.get_arg(key)
    if v:
        #logger.debug(f"Found {key} in taskData.args: '{v}'")
        return v
    # Check if the key exists in the user secrets
    v = taskData.Secrets.get(key)
    if v:
        #logger.debug(f"Found {key} in taskData.Secrets: '{v}'")
        return v
    # Check if the key exists in the payload build parameters
    for param in taskData.BuildParameters:
        if param.Name == key:
            v = param.Value
            if v:
                #logger.debug(f"Found {key} in taskData.BuildParameters: '{v}'")
                return v
    # Check if the key exists in the payload container environment variables
    v = os.getenv(key)
    if v:
        #logger.debug(f"Found {key} in environment variables: '{v}'")
        return v
    logger.debug(f"Did not find {key} in taskData.args, taskData.Secrets, taskData.BuildParameters, or environment variables")
    return None


async def ensure_bloodhound_task_preflight(task_id: int) -> tuple[bool, str]:
    """Connect BloodHound before a task builds its model graph.

    Query and chat both need the same graph oracle. Keep this fail-soft so an
    unavailable MCP does not stop Sage from answering non-graph requests, but
    surface the same EventFeed warning for either entrypoint.
    """
    try:
        from ai.bloodhound_config import ensure_bloodhound_connected, BLOODHOUND_SETUP_STEPS

        connected, message = await ensure_bloodhound_connected()
        logger.info(f"BloodHound auto-connect: {message}")
        if not connected:
            try:
                await SendMythicRPCOperationEventLogCreate(MythicRPCOperationEventLogCreateMessage(
                    TaskID=task_id,
                    Warning=True,
                    Message=(
                        "Sage could not auto-connect BloodHound — attack-graph ingest/analysis are unavailable. "
                        + BLOODHOUND_SETUP_STEPS
                    ),
                ))
            except Exception as exc:
                logger.debug(f"BloodHound EventFeed notice failed: {exc}")
        return connected, message
    except Exception as exc:
        logger.debug(f"BloodHound auto-connect skipped: {exc}")
        return False, f"BloodHound auto-connect skipped: {exc}"
