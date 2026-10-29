// =============================================================================
//  dns_server.h  --  captive-portal DNS: answers every A query with the AP's
//  address (192.168.4.1), so phones open the setup page by themselves.
// =============================================================================
#pragma once

void dns_server_start(void);
void dns_server_stop(void);
