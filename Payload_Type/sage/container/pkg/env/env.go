package env

import (
	// Standard
	"fmt"
	"os"

	// Mythic
	structs "github.com/MythicMeta/MythicContainer/agent_structs"
	"github.com/MythicMeta/MythicContainer/logging"
)

// Get retrieves the value for a given key from the task, user secrets, payload build parameters, or payload container environment variables.
// It checks the following order:
// 1. Task Level
// 2. User Secrets
// 3. Payload Build Parameters
// 4. Payload Container Environment Variables
// If the key is not found in any of these, it returns an error.
func Get(task *structs.PTTaskMessageAllData, key string) (value string, err error) {
	logging.LogDebug(fmt.Sprintf("Getting value for key: %s", key))

	// Check if the key exists in the task
	value, err = task.Args.GetChooseOneArg(key)
	if err == nil {
		if value != "" {
			logging.LogDebug(fmt.Sprintf("Key %s found in task args", key))
			return value, nil
		}
	}
	logging.LogDebug(fmt.Sprintf("Key %s not found in task args", key))

	// Check if the key exists in the user secrets
	v, ok := task.Secrets[key]
	if ok {
		if v.(string) != "" {
			logging.LogDebug(fmt.Sprintf("Key %s found in user secrets", key))
			return v.(string), nil
		}
	}
	logging.LogDebug(fmt.Sprintf("Key %s not found in user secrets", key))

	// Check if the key exists in the payload build parameters
	for _, param := range task.BuildParameters {
		if param.Name == key {
			if param.Value != "" {
				logging.LogDebug(fmt.Sprintf("Key %s found in payload build parameters", key))
				return param.Value.(string), nil
			}
		}
	}
	logging.LogDebug(fmt.Sprintf("Key %s not found in payload build parameters", key))

	// Check if the key exists in the payload container environment variables
	value = os.Getenv(key)
	if value != "" {
		logging.LogDebug(fmt.Sprintf("Key %s found in payload container environment variables", key))
		return value, nil
	} else {
		logging.LogDebug(fmt.Sprintf("Key %s not found in payload container environment variables", key))
		err = fmt.Errorf("key %s not found in task args, user secrets, payload build parameters, or payload container environment variables", key)
		return "", err
	}
}
